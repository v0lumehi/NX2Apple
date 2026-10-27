#!/usr/bin/env python3
"""Sync this fork with upstream seedds/nxnative.

The fork was started as a fresh repository (no shared git history), so GitHub's own
"Sync fork" does not apply. The sync is a mechanical procedure — take upstream's tree,
re-apply the fork's features, verify the result builds — and this script is that
procedure, so a scheduled CI job can run it unattended.

What it does:
  1. diffs the two upstream refs over the vendored paths (dm/ scripts/ docs/ AGENTS.md ref/), and
     adds any vendored file upstream has that the fork's tree lacks
  2. applies adds and modifications file by file, and deletions — except the fork's own
     files (the RomFS archive layer), which upstream must not delete
  3. re-applies the fork's patches to the four files that carry them
     (hle_fs.c, the host Makefile, build_ipa.sh, the NSP script); every patch is
     anchor-based and idempotent, and one that no longer fits fails the run loudly
     rather than producing a silently broken tree
  4. verifies: every .py parses on the Python this fork supports as its floor (3.9),
     the iOS build script survives bash -n, the archive reader test passes, and the
     macOS host builds for a real profile (skippable with --skip-build)

On success the caller commits and pushes; on failure nothing is written outside the
working tree, so CI can open an issue with the log instead.

Usage: sync_upstream.py --old <upstream sha> --new <upstream sha> [--skip-build]
"""
import argparse, json, os, pwd, re, subprocess, sys, shutil, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDORED = ('dm', 'scripts', 'docs', 'AGENTS.md', 'ref')
# The fork's own files, inside vendored paths: upstream deletes and rewrites must not touch them.
OURS = {'dm/host/romfs_arc.c', 'dm/host/romfs_arc.h', 'dm/tools/romfs_arc.py',
        'dm/host/tests/t_romfs_arc.c', 'docs/ROMFS_ARCHIVE.md',
        'dm/games/0100A00019DE0000/game.fork.tsv',
        # BC decode for GPUs without BC support (from d2r-mobile's bcdec line; tex_bc_cpu profiles)
        'dm/third_party/bcdec/bcdec.h', 'dm/third_party/bcdec/LICENSE',
        'dm/gfx/tex/nvn_bcdec.c', 'dm/gfx/tex/nvn_bcdec.h',
        # The adjustable clock entry (PR #26) and its test are fork-only for now: upstream does not
        # have CreateWithAdjustableSystemClock, so a sync would otherwise replace these files and
        # silently drop the fix (issue #29, found by v0lumehi reading the sync script). If upstream
        # takes seedds PR for it, these leave OURS and come back with the sync instead.
        'dm/host/hle_time.c', 'dm/host/tests/t_time.c',
        # dm/games/game.py carries the tex_bc_cpu profile key and the 'profile' subcommand that
        # writes the runtime profile; both are fork-only, so the file is never taken from upstream
        # (a verify-only patcher was not enough: the sync replaced it first and then complained).
        'dm/games/game.py',
        'dm/gfx/tex/tests/t_bcdec.c',
        # The Increased Memory Limit guide the README links to; upstream has no such file, and the
        # first wholesale sync deleted it.
        'dm/ios/ENTITLEMENTS.md',
        # This script: fork-only, so a PR that changes it is not a vendored edit (pr-scope reads OURS)
        'scripts/sync_upstream.py',
        # The NRO bound's tests (issue #14): fork-only, beside nso_code.py's lo bound
        'dm/tools/tests/test_nro_header.py'}
# Vendored files that carry fork patches: taken fresh from upstream, then patched below.
PATCHED = ('dm/host/hle_fs.c', 'dm/host/Makefile', 'dm/host/tests/Makefile', 'dm/ios/tools/build_ipa.sh',
           'scripts/build_ipa_from_nsp.py', 'dm/gfx/tools/shader_split.py',
           'dm/ios/Sources/main.m', 'dm/gfx/nvn/nvn_mtl.m', 'dm/host/dm.h',
           'dm/ios/tests/test_romfs_startup.py', 'dm/ios/tests/test_profile_pack.py', 'docs/env_switches.tsv')

def git(*a):
    r = subprocess.run(['git', *a], cwd=REPO, capture_output=True, text=True)
    if r.returncode: sys.exit(f'git {" ".join(a)} failed: {r.stderr.strip()}')
    return r.stdout

def blob(rev, path):
    r = subprocess.run(['git', 'cat-file', 'blob', f'{rev}:{path}'], cwd=REPO, capture_output=True)
    return r.stdout if r.returncode == 0 else None

def upstream_changes(old, new):
    """(kind, path) for every vendored file that differs, kind in AM D; plus an A for every vendored
    file upstream has that the fork's tree lacks, and an M for an upstream executable that lost its x bit here. The diff alone only sees files that changed between
    the two refs, so a file the first sync missed never arrived: docs/geometry-metal-research.md (which
    game notes link to) and 34 of docs/images, until test_docs.py found the dead links."""
    out = git('diff', '--name-status', old, new, '--', *VENDORED).splitlines()
    changes = []
    for line in out:
        parts = line.split('\t')
        kind, path = parts[0][0], parts[-1]
        changes.append((kind, path))
    named = {p for _, p in changes}
    for line in git('ls-tree', '-r', new, '--', *VENDORED).splitlines():
        meta, path = line.split('\t', 1)
        if path in named or path in OURS: continue
        dest = REPO / path
        if not dest.exists(): changes.append(('A', path))
        elif meta.split()[0] == '100755' and not os.access(dest, os.X_OK): changes.append(('M', path))   # an executable that lost its x bit
    return changes

# ---- the fork's patches. Each is (name, already-marker, [(old, new), ...]); a patch whose
# marker is present is skipped, one whose old text is absent fails the run. ----

HLE = 'dm/host/hle_fs.c'
def patch_hle_fs(t):
    P = []
    P.append(('include', '#include "romfs_arc.h"', [(
        '#include "hle.h"\n#include <mach/mach.h>\n#include "front.h"\n',
        '#include "hle.h"\n#include <mach/mach.h>\n#include "front.h"\n#include "romfs_arc.h"\n')]))
    P.append(('synthetic path', 'ROMFSARC_SYNTH_PREFIX "%s"', [(
        '        if (g_mnt[i].overlay[0] && !create) return rom_overlay_path(&g_mnt[i], rest, out, n);',
        '''        if (g_mnt[i].overlay[0] && !create) return rom_overlay_path(&g_mnt[i], rest, out, n);
        // Archive mount: the RomFS tree lives inside a ROMFSARC archive, so the host path is a
        // synthetic "@romfsarc:<guest path>" that the file calls below route to romfs_arc.c. Matched
        // by the mount directory (romfsarc_open recorded the base it actually opened), because the
        // mount name differs between games (rom, content).
        if (romfsarc_active() && g_mnt[i].ro && !create && g_mnt[i].dir[0] && !strcmp(g_mnt[i].dir, g_arc_base)) {
            // Fold runs of '/' the way the directory walk below does: the archive index stores one
            // separator per level, and a game may ask for "data:/bin/appli/icon_sick//bin/x.arc"
            // (Pokemon Sword's Bag: the doubled slash missed the lookup and the game then
            // dereferenced the null it got back — PR #9).
            char folded[1024]; const char *r = rest;
            if (strstr(rest, "//")) {
                size_t o = 0; int slash = 0;
                for (const char *q = rest; *q; q++) {
                    if (*q == '/') { if (slash) continue; slash = 1; } else slash = 0;
                    if (o + 1 >= sizeof folded) { errno = ENAMETOOLONG; return 0; }
                    folded[o++] = *q;
                }
                folded[o] = 0; r = folded;
            }
            if (snprintf(out, n, ROMFSARC_SYNTH_PREFIX "%s", r) >= (int)n) { errno = ENAMETOOLONG; return 0; }
            return 1;
        }''')]))
    P.append(('MountRom', 'romfsarc_open(dir, &h)', [(
        '''HLE_FORCE("_ZN2nn2fs8MountRomEPKcPvm", "fsp-srv; rom → host RomFS directory", Result, h_fs_MountRom, const char *name, void *cache, size_t sz) {
    (void)cache; (void)sz; char b[1024];
    Result r = mount(name, envdir("DM_ROMFS", "romfs", b, sizeof b), 1);''',
        '''HLE_FORCE("_ZN2nn2fs8MountRomEPKcPvm", "fsp-srv; rom → host RomFS directory (or a ROMFSARC archive)", Result, h_fs_MountRom, const char *name, void *cache, size_t sz) {
    (void)cache; (void)sz; char b[1024];
    const char *dir = envdir("DM_ROMFS", "romfs", b, sizeof b);
    // Archive mount: romfsarc_open tries <dir>.idx, <dir>/romfs.idx and <dir>/romfs/romfs.idx, so the
    // player may copy the shards either beside the folder or into Documents/romfs/ itself.
    romfsarc_hdr h;
    if (romfsarc_open(dir, &h) == 0) {
        // Mount the resolved archive base (romfsarc_open recorded the one that worked in g_arc_base).
        // host_path_ex compares the mount directory against it.
        Result r = mount(name, g_arc_base, 1);
        if (r != RES_OK) { romfsarc_close(); return r; }
        LOG("fs rom: archive %s (%llu files, %llu dirs, %u shards, tree hash %016llx)", g_arc_base,
            (unsigned long long)h.file_count, (unsigned long long)h.dir_count, h.shard_count, (unsigned long long)h.tree_hash);
        return RES_OK;
    }
    Result r = mount(name, dir, 1);''')]))
    P.append(('archive handles', 'static uint64_t arc_new', [(
        'HLE_FORCE("_ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci", "fsp-srv → open(2)", Result, h_fs_OpenFile, uint64_t *out, const char *path, int mode) {',
        '''// ---------- Archive-backed file handles ----------
// An archive file has no host fd. It gets a handle number from a private table, and every operation
// on it (ReadFile, GetFileSize, CloseFile) checks the table first. Handle numbers are distinct from
// fds: real handles are fds (>= 3 in practice), archive handles are tagged by index into g_arc[].
typedef struct { uint32_t shard; uint64_t off, size; int used; } archandle;
#define MAX_ARCH 4096
static archandle g_arc[MAX_ARCH];
static uint64_t g_arch_gen;   // bumped on open/close so a stale handle never hits a recycled slot
static uint64_t arc_new(uint32_t shard, uint64_t off, uint64_t size) {
    for (int i = 0; i < MAX_ARCH; i++) if (!g_arc[i].used) {
        g_arc[i].used = 1; g_arc[i].shard = shard; g_arc[i].off = off; g_arc[i].size = size;
        return ((uint64_t)1 << 63) | ((g_arch_gen & 0x7fffffff) << 32) | (uint32_t)i;
    }
    return 0;
}
static int arc_get(uint64_t h, archandle **out) {
    if (!(h >> 63)) return 0;
    uint32_t i = (uint32_t)h;
    if (i >= MAX_ARCH || !g_arc[i].used) return 0;
    if ((uint32_t)((h >> 32) & 0x7fffffff) != (uint32_t)(g_arch_gen & 0x7fffffff)) return 0;
    *out = &g_arc[i]; return 1;
}
static void arc_free(uint64_t h) {
    archandle *a; if (arc_get(h, &a)) a->used = 0;
}
static const char *arc_rel(const char *hp) {
    const char *rel = ROMFSARC_SYNTH_REL(hp);
    while (*rel == '/') rel++;
    return rel;
}
static int arc_file(const char *hp, uint32_t *shard, uint64_t *off, uint64_t *size) {
    return romfsarc_stat_file(arc_rel(hp), shard, off, size);
}
static int arc_dir(const char *hp) { return romfsarc_is_dir(arc_rel(hp)); }
static void arc_norm(const char *hp, char *rel, size_t n) { strlcpy(rel, arc_rel(hp), n); }
HLE_FORCE("_ZN2nn2fs8OpenFileEPNS0_10FileHandleEPKci", "fsp-srv → open(2)", Result, h_fs_OpenFile, uint64_t *out, const char *path, int mode) {''')]))
    P.append(('OpenFile', 'arc_new(shard, off, size)', [(
        '''    HP(path);
    if (mode & (MODE_WRITE | MODE_APPEND)) RW();
    int fd = open(hp, (mode & (MODE_WRITE | MODE_APPEND)) ? ((mode & MODE_READ) ? O_RDWR : O_WRONLY) : O_RDONLY);''',
        '''    HP(path);
    if (mode & (MODE_WRITE | MODE_APPEND)) RW();
    if (ROMFSARC_IS_SYNTH(hp)) {   // archive entry: no fd, the handle carries shard/offset/size
        uint32_t shard; uint64_t off, size;
        if (!arc_file(hp, &shard, &off, &size)) { errno = ENOENT; return err_res("open", hp); }
        uint64_t ah = arc_new(shard, off, size);
        if (!ah) { errno = EMFILE; return err_res("open", hp); }
        *out = ah;
        if (g_flag.fs) LOG("fs open %s mode=%d → archive handle (shard %u off %llu size %llu)", path, mode, shard, (unsigned long long)off, (unsigned long long)size);
        return RES_OK;
    }
    int fd = open(hp, (mode & (MODE_WRITE | MODE_APPEND)) ? ((mode & MODE_READ) ? O_RDWR : O_WRONLY) : O_RDONLY);''')]))
    P.append(('ReadFile', 'romfsarc_pread(ah->shard', [(
        '''    size_t got = 0; int reprep = 0, fd = (int)h; int64_t base = 0;
    if (IMG_H(h)) {   // an image entry: read the image's descriptor at the entry's offset, never past its end
        const img_ent *x = &g_mnt[IMG_M(h)].img->e[IMG_K(h)];
        fd = g_mnt[IMG_M(h)].img->fd; base = (int64_t)x->a;
        sz = off < 0 || (uint64_t)off >= x->b ? 0 : (size_t)(x->b - off) < sz ? (size_t)(x->b - off) : sz;
    }
    dm_guest_write_prepare(buf, sz);   // The destination may be a texture pool page write-protected by the graphics layer: a kernel write to a read-only page returns EFAULT instead of page-faulting, so unprotect and mark dirty first
    while (got < sz) {''',
        '''    size_t got = 0; int reprep = 0, fd = (int)h; int64_t base = 0;
    if (IMG_H(h)) {   // an image entry: read the image's descriptor at the entry's offset, never past its end
        const img_ent *x = &g_mnt[IMG_M(h)].img->e[IMG_K(h)];
        fd = g_mnt[IMG_M(h)].img->fd; base = (int64_t)x->a;
        sz = off < 0 || (uint64_t)off >= x->b ? 0 : (size_t)(x->b - off) < sz ? (size_t)(x->b - off) : sz;
    }
    dm_guest_write_prepare(buf, sz);   // The destination may be a texture pool page write-protected by the graphics layer: a kernel write to a read-only page returns EFAULT instead of page-faulting, so unprotect and mark dirty first
    archandle *ah;
    if (arc_get(h, &ah)) {   // archive entry: clamp to the file, read from its shard
        if (off < 0) { *out = 0; return RES_FAIL; }
        uint64_t avail = (uint64_t)off < ah->size ? ah->size - (uint64_t)off : 0;
        size_t want = sz < avail ? sz : (size_t)avail;
        if (want) { ssize_t r = romfsarc_pread(ah->shard, ah->off + (uint64_t)off, buf, want);
            if (r < 0) return err_res("pread (archive)", "handle"); got = (size_t)r; }
        *out = got;
        if (g_flag.fs) { static uint64_t n, bytes, next_b = 64 << 20; static pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
            pthread_mutex_lock(&m); n++; bytes += got;
            if (bytes >= next_b || n % 1000 == 0) { LOG("fs read total: %llu reads %.1f MiB (this one: archive offset %lld length %#zx)", n, bytes / 1048576.0, (long long)off, got); if (bytes >= next_b) next_b += 64 << 20; }
            pthread_mutex_unlock(&m); }
        return RES_OK;
    }
    while (got < sz) {''')]))
    P.append(('GetFileSize', 'ah->size; return RES_OK', [(
        '''HLE_FORCE("_ZN2nn2fs11GetFileSizeEPlNS0_10FileHandleE", "fsp-srv → fstat(2)", Result, h_fs_GetFileSize, int64_t *out, uint64_t h) {
    if (!h) return RES_FAIL;''',
        '''HLE_FORCE("_ZN2nn2fs11GetFileSizeEPlNS0_10FileHandleE", "fsp-srv → fstat(2)", Result, h_fs_GetFileSize, int64_t *out, uint64_t h) {
    if (!h) return RES_FAIL;
    archandle *ah; if (arc_get(h, &ah)) { *out = (int64_t)ah->size; return RES_OK; }''')]))
    P.append(('CloseFile', 'arc_free(h)', [(
        '''HLE_FORCE("_ZN2nn2fs9CloseFileENS0_10FileHandleE", "fsp-srv → close(2)", void, h_fs_CloseFile, uint64_t h) {
    if (!h || IMG_H(h)) return;   // the null handle after a failed open (see ReadFile): do not touch fd 0; an image entry holds no descriptor''',
        '''HLE_FORCE("_ZN2nn2fs9CloseFileENS0_10FileHandleE", "fsp-srv → close(2)", void, h_fs_CloseFile, uint64_t h) {
    if (!h || IMG_H(h)) return;   // the null handle after a failed open (see ReadFile): do not touch fd 0; an image entry holds no descriptor
    if (h >> 63) { arc_free(h); return; }   // archive entry: nothing to close''')]))
    P.append(('GetEntryType', 'romfsarc_stat_file(rel', [(
        '''HLE_FORCE("_ZN2nn2fs12GetEntryTypeEPNS0_18DirectoryEntryTypeEPKc", "fsp-srv → stat(2)", Result, h_fs_GetEntryType, int32_t *out, const char *path) {
    int mi; int64_t k;
    if (img_path(path, &mi, &k)) { if (k < 0) return err_res("stat", path); *out = g_mnt[mi].img->e[k].type ? ENT_FILE : ENT_DIR; return RES_OK; }
    HP(path); struct stat st;''',
        '''HLE_FORCE("_ZN2nn2fs12GetEntryTypeEPNS0_18DirectoryEntryTypeEPKc", "fsp-srv → stat(2)", Result, h_fs_GetEntryType, int32_t *out, const char *path) {
    int mi; int64_t k;
    if (img_path(path, &mi, &k)) { if (k < 0) return err_res("stat", path); *out = g_mnt[mi].img->e[k].type ? ENT_FILE : ENT_DIR; return RES_OK; }
    HP(path); struct stat st;
    if (ROMFSARC_IS_SYNTH(hp)) {   // archive entry or directory, from the index
        const char *rel = arc_rel(hp);   // the index stores paths without a leading slash (abe35a3)
        uint32_t sh; uint64_t of, szf;
        if (romfsarc_stat_file(rel, &sh, &of, &szf)) { *out = ENT_FILE; return RES_OK; }
        if (romfsarc_is_dir(rel)) { *out = ENT_DIR; return RES_OK; }
        errno = ENOENT; return err_res("stat", hp);
    }''')]))
    # Runtime profile: the save-exists check compares the program id against dm_profile()'s (the
    # compiled macro is what dm_profile() falls back to; merged with the stage-2 experiment).
    P.append(('save-exists profile', 'dm_profile()->program_id', [(
        # both occurrences on the line: the log message and the return value
        'app == DM_GAME_PROGRAM_ID ? "this game, yes" : "another program, no"); return app == DM_GAME_PROGRAM_ID;',
        'app == dm_profile()->program_id ? "this game, yes" : "another program, no"); '
        'return app == dm_profile()->program_id;')]))
    P.append(('snapshot helper', 'static void arc_snapshot', []))   # inserted below, needs dirh_t
    P.append(('OpenDirectory', 'arc_snapshot(ah, hp)', [(
        '''    HP(path); DIR *d = opendir(hp);
    if (!d) return err_res("opendir", hp);''',
        '''    HP(path);
    if (ROMFSARC_IS_SYNTH(hp)) {   // archive directory: no host DIR, only the snapshot
        if (!arc_dir(hp)) { errno = ENOTDIR; return err_res("opendir", hp); }
        dirh_t *ah = calloc(1, sizeof *ah); if (!ah) die("directory handle allocation failed");
        strlcpy(ah->path, hp, sizeof ah->path); ah->mode = mode;
        arc_snapshot(ah, hp);
        *out = (uint64_t)ah; return RES_OK;
    }
    DIR *d = opendir(hp);
    if (!d) return err_res("opendir", hp);''')]))
    P.append(('ReadDirectory guard', "an archive directory that turned out empty", [(
        '''    int dfd = dirfd(h->d);''',
        '''    if (!h->d) { *count = 0; return RES_OK; }   // an archive directory that turned out empty (no snapshot, no host DIR)
    int dfd = dirfd(h->d);''')]))
    P.append(('CloseDirectory guard', 'if (h->d) closedir(h->d)', [(
        '''    dirh_t *h = (dirh_t *)hh; closedir(h->d); if (h->od) closedir(h->od); free(h->snap); free(h);''',
        '''    dirh_t *h = (dirh_t *)hh; if (h->d) closedir(h->d); if (h->od) closedir(h->od); free(h->snap); free(h);''')]))

    for name, marker, pairs in P:
        if marker in t:
            skip(f'hle_fs.c {name} (already patched)'); continue
        for old, new in pairs:
            if old not in t: fail(f'{HLE}: patch "{name}" anchor missing: {old[:70]!r}')
            t = t.replace(old, new, 1)
        done(f'hle_fs.c {name}')
    # arc_snapshot goes right after the dirh_t typedef: it names the type
    if 'static void arc_snapshot' not in t:
        import re
        m = re.search(r'typedef struct \{ DIR \*d;.*?\} dirh_t;\n', t, re.S)
        if not m: fail(f'{HLE}: dirh_t typedef not found')
        t = t[:m.end()] + SNAPSHOT + t[m.end():]
        done('hle_fs.c arc_snapshot')
    return t

SNAPSHOT = '''
// An archive directory has no host DIR: its entries come from the index, copied into the same
// snapshot list the overlay mount uses, so ReadDirectory has one path for both.
static void arc_snapshot(dirh_t *h, const char *hp) {
    char rel[1024]; arc_norm(hp, rel, sizeof rel);
    int n = romfsarc_count(rel);
    if (n <= 0) return;
    romfsarc_ent *ents = malloc((size_t)n * sizeof *ents);
    if (!ents) die("archive directory snapshot allocation failed");
    int got = romfsarc_list(rel, ents, n);
    if (got <= 0) { free(ents); return; }
    h->snap = calloc((size_t)got, sizeof *h->snap); h->nsnap = got;
    if (!h->snap) die("archive directory snapshot allocation failed");
    for (int i = 0; i < got; i++) {
        strlcpy(h->snap[i].name, ents[i].name, sizeof h->snap[i].name);
        h->snap[i].type = ents[i].is_dir ? ENT_DIR : ENT_FILE;
        h->snap[i].size = ents[i].is_dir ? 0 : ents[i].size;
    }
    free(ents);
}
'''

def patch_makefile(t):
    if 'romfs_arc.c' in t: return skip('Makefile (already patched)')
    # Anchor on the stable SRC prefix: upstream adds and reorders files after hle.c (haptics_apple.m)
    old = 'SRC=main.c loader.c hle.c '
    if old not in t: fail('dm/host/Makefile: SRC anchor missing (upstream changed the source list)')
    return done('Makefile SRC', t.replace(old, 'SRC=main.c loader.c hle.c romfs_arc.c ', 1))

TESTS_MK = 'dm/host/tests/Makefile'
def patch_tests_makefile(t):
    """The three fs tests #include ../hle_fs.c, which now calls the archive reader: without
    romfs_arc.c on their command lines they stop with undefined _romfsarc_* symbols (PR #12)."""
    if 'romfs_arc.c' in t: return skip('tests/Makefile (already patched)')
    P = [
        ('t_fs', 'clang -O1 -Wno-unused-function -I.. $(GINC) -o $@ t_fs.c t_fs_stub.c ../hle_bigstack.c\n',
         'clang -O1 -Wno-unused-function -I.. $(GINC) -o $@ t_fs.c t_fs_stub.c ../hle_bigstack.c ../romfs_arc.c\n'),
        ('t_fs_commit_list', 'clang -O1 -Wall -Wno-unused-function -I.. $(GINC) -o $@ t_fs_commit_list.c t_fs_stub.c ../hle_bigstack.c\n',
         'clang -O1 -Wall -Wno-unused-function -I.. $(GINC) -o $@ t_fs_commit_list.c t_fs_stub.c ../hle_bigstack.c ../romfs_arc.c\n'),
        ('t_fs_case', 'clang -O1 -Wno-unused-function -I.. $(GINC) -o $@ t_fs_case.c ../hle_bigstack.c\n',
         'clang -O1 -Wno-unused-function -I.. $(GINC) -o $@ t_fs_case.c ../hle_bigstack.c ../romfs_arc.c\n'),
    ]
    for name, old, new in P:
        if old not in t: fail(f'{TESTS_MK}: {name} link line missing (upstream changed the test rules)')
        t = t.replace(old, new, 1)
    # the touch-pad geometry test (dm/host/touchpad_geometry.h): header-only, so it needs its own
    # rule and a place in the run list
    if 't_touchpad_geometry' not in t:
        run_old = '$(B)/t_haptics $(B)/t_haptics_apple\n'
        if run_old not in t: fail(f'{TESTS_MK}: run list anchor missing')
        t = t.replace(run_old, '$(B)/t_haptics $(B)/t_haptics_apple $(B)/t_touchpad_geometry\n', 1)
        body_old = '\t$(B)/t_haptics_apple\n'
        if body_old not in t: fail(f'{TESTS_MK}: run body anchor missing')
        t = t.replace(body_old, body_old + '\t$(B)/t_touchpad_geometry\n', 1)
        rule_anchor = '$(B)/t_haptics_apple: t_haptics_apple.m'
        if rule_anchor not in t: fail(f'{TESTS_MK}: t_haptics_apple rule missing')
        t = t.replace(rule_anchor,
                      '$(B)/t_touchpad_geometry: t_touchpad_geometry.c ../touchpad_geometry.h ../dm_input.h | $(B)\n'
                      '\tclang -O1 -Wall -I.. $(GINC) -o $@ t_touchpad_geometry.c\n' + rule_anchor, 1)
        done('tests/Makefile touchpad test')
    # the prerequisites too, so a change to the reader rebuilds them — and hle_profile.h, so a
    # change to the runtime profile does
    for name in ('t_fs', 't_fs_commit_list', 't_fs_case'):
        dep_old = f'$(B)/{name}:'
        i = t.index(dep_old)
        j = t.index('\n', i)
        line = t[i:j]
        if '../romfs_arc.c' not in line:
            line = line.replace(' ../hle.h', ' ../romfs_arc.c ../hle.h', 1)
        if '../hle_profile.h' not in line:
            line = line.replace(' | $(B)', ' ../hle_profile.c ../hle_profile.h | $(B)', 1)
        t = t[:i] + line + t[j:]
    # hle_profile.c: every test whose prerequisites reach a dm_profile() caller needs the accessor
    # at link time (the runtime-profile merge added those calls; the sync replaces this file, so the
    # rule has to live here or the host tests stop linking after every sync).
    USERS = ('../hle_svc.c', '../hle_thread.c', '../hle_fs.c', '../loader.c',
             '../hle_fpslock.c', '../hle_oe.c', '../hle_os.c', '../hle_sdk16.c')
    lines = t.splitlines(keepends=True)
    out, need = [], False
    for ln in lines:
        if ln.startswith('$(B)/'):
            # a rule header: does its prerequisite list reach a dm_profile() caller?
            need = any(u in ln for u in USERS)
            out.append(ln)
            continue
        if need:
            # the recipe line of that rule: append the accessor's object if absent
            if ln.startswith('\t') and ln.lstrip().startswith('clang') and 'hle_profile.c' not in ln:
                ln = ln.rstrip('\n') + ' ../hle_profile.c\n'
            elif not ln.startswith('\t') and ln.strip():
                need = False          # a non-recipe line ends the rule
        out.append(ln)
    t = ''.join(out)
    # the memory block compiles ../hle_svc.c from MEM_DEPS
    mem_old = 'MEM_DEPS = t_svc_memory.c memory_profile/game_profile.h ../hle_svc.c ../hle_guestthread.c ../hle.h ../dm.h'
    if mem_old in t and 'hle_profile.c' not in t.split(mem_old)[1].split('\n')[0]:
        t = t.replace(mem_old, mem_old + ' ../hle_profile.c ../hle_profile.h', 1)
        for tail in ('t_svc_memory.c\n', 't_svc_memory.c ../../gfx/nvn/nvn_dirty.c\n'):
            t = t.replace('\t' + 'clang $(MEM_FLAGS)', '\t' + 'clang $(MEM_FLAGS)')  # no-op guard
        import re as _re
        t = _re.sub(r'(\tclang \$\(MEM_FLAGS\)[^\n]*?\.c)(\n)',
                    lambda m: m.group(1) + ' ../hle_profile.c' + m.group(2), t)
    return done('tests/Makefile romfs_arc.c + hle_profile.c', t)

ENT = '''# The unsigned IPA: an unsigned binary has no signature for a sideloader to read entitlements from,
# so the request would be lost (AltStore reads the signature it replaces). Sign ad-hoc with the
# entitlement first; the sideloader strips this signature and carries the entitlement over.
if [ "$PREPARE_XCODE" != YES ] && [ -n "${DM_ENTITLEMENTS-default}" ]; then
  if [ -n "${DM_ENTITLEMENTS:-}" ]; then cp "$DM_ENTITLEMENTS" "$APP/DmIOS.entitlements" || die "copy DM_ENTITLEMENTS"
  else printf '%s\\n' '<?xml version="1.0" encoding="UTF-8"?>' '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \\
    '<plist version="1.0"><dict><key>com.apple.developer.kernel.increased-memory-limit</key><true/></dict></plist>' > "$APP/DmIOS.entitlements" || die "entitlements"; fi
  # codesign refuses a bundle whose files carry resource forks, Finder information, quarantine or
  # provenance attributes ("resource fork, Finder information, or similar detritus not allowed");
  # files copied into the bundle (the icon, resources staged from a Downloads folder) can bring them
  xattr -cr "$APP" 2>/dev/null
  codesign --force --sign - --entitlements "$APP/DmIOS.entitlements" "$APP" || die "ad-hoc sign with entitlements (the sideloader re-signs; this only carries the request)"
  codesign -d --entitlements :- "$APP" 2>/dev/null | grep -q increased-memory-limit || die "entitlements did not make it into the placeholder signature"
  t "entitlements: increased-memory-limit embedded in the placeholder signature"
fi
'''

def patch_build_ipa(t):
    if 'placeholder signature' in t: return skip('build_ipa.sh entitlements (already patched)')
    old = '''APP=$DD/Build/Products/Release-iphoneos/DmIOS.app
[ -n "$APP" ] && [ -d "$APP" ] || die "no built app"; t "app: $APP"
'''
    if old not in t: fail('dm/ios/tools/build_ipa.sh: APP anchor missing (upstream moved the app definition)')
    t = t.replace(old, old + ENT, 1)
    # The RomFS mapping moved into the `P=…; MAPS=(…)` assignment (upstream folded it in when it
    # added the pso/ mapping), so the anchor is the assignment plus the line that follows the
    # whole block — not the two adjacent lines the old form had. Both spellings are accepted: a
    # checkout from before that change still syncs.
    slim = '''# DM_FULL_ROMFS=1 packs the RomFS into the package (upstream behaviour; only sensible for small
# games — a full package of a large game is many GB and iOS refuses to install it with
# PackageExtractionFailed). The default is the slim package: the app reads the game files from
# Documents/romfs (a directory tree, or a ROMFSARC archive: romfs.idx + romfs.NN shards).'''
    # Upstream picks the mapping form by what the RomFS path is (a .img file or a folder)
    old2img = '''if [ -f "$ROMFS" ]; then MAPS=("$P/romfs.img=$ROMFS"); else MAPS=("$P/romfs=$ROMFS"); fi'''
    if old2img in t:
        t = t.replace(old2img, '''MAPS=()
# DM_FULL_ROMFS=1 packs the game files into the package (upstream behaviour; only sensible for small
# games — a full package of a large game is many GB and iOS refuses to install it with
# PackageExtractionFailed). The default is the slim package: the app reads them from
# Documents/games/<id>/ (romfs.img and aoc/<index>.img), which the launcher copies in one go and
# which can also live on an external drive.
if [ -n "${DM_FULL_ROMFS:-}" ]; then if [ -f "$ROMFS" ]; then MAPS=("$P/romfs.img=$ROMFS"); else MAPS=("$P/romfs=$ROMFS"); fi; fi''', 1)
        # Add-on content follows the same rule: with the slim default it goes to
        # Documents/games/<id>/aoc/<index>.img (the host already mounts *.img there, hle_fs.c
        # aoc_dir), so the IPA stays small and the game data is one folder the user copies.
        old_aoc = '''if [ -n "$AOC" ] && [ -d "$AOC" ]; then MAPS+=("$P/aoc=$AOC"); t "add-on content: $(ls "$AOC" | tr '\\n' ' ')"; fi'''
        if old_aoc in t and 'if [ -n "${DM_FULL_ROMFS:-}" ] && [ -n "$AOC" ]' not in t:
            t = t.replace(old_aoc, '''if [ -n "${DM_FULL_ROMFS:-}" ] && [ -n "$AOC" ] && [ -d "$AOC" ]; then
  MAPS+=("$P/aoc=$AOC"); t "add-on content packed into the IPA: $(ls "$AOC" | tr '\\n' ' ')"
elif [ -n "$AOC" ] && [ -d "$AOC" ]; then
  t "add-on content alongside the game data (DM_FULL_ROMFS=1 packs it): $(ls "$AOC" | tr '\\n' ' ')"
fi''', 1)
        old3 = '''t "pack IPA (already-compressed game files stored, everything else deflated)"'''
        if old3 not in t: fail('dm/ios/tools/build_ipa.sh: pack line missing after the MAPS assignment')
        t = t.replace(old3, '''if [ -n "${DM_FULL_ROMFS:-}" ]; then
  t "pack full IPA with RomFS (already-compressed game files stored, everything else deflated)"
else
  t "pack slim IPA without RomFS (game files come from Documents/romfs; DM_FULL_ROMFS=1 packs them)"
fi''', 1)
        return done('build_ipa.sh slim + entitlements', t)
    old2 = '''P=Payload/DmIOS.app; MAPS=("$P/romfs=$ROMFS")'''
    if old2 in t:
        # current upstream: drop the RomFS from the initial assignment, add it back inside the
        # DM_FULL_ROMFS branch, and leave every other mapping (aoc, pso, msl, app) untouched
        t = t.replace(old2, '''P=Payload/DmIOS.app; MAPS=()
# DM_FULL_ROMFS=1 packs the RomFS into the package (upstream behaviour; only sensible for small
# games — a full package of a large game is many GB and iOS refuses to install it with
# PackageExtractionFailed). The default is the slim package: the app reads the game files from
# Documents/romfs (a directory tree, or a ROMFSARC archive: romfs.idx + romfs.NN shards).
if [ -n "${DM_FULL_ROMFS:-}" ]; then MAPS=("$P/romfs=$ROMFS"); fi''', 1)
        old3 = '''t "pack IPA (already-compressed game files stored, everything else deflated)"'''
        if old3 not in t: fail('dm/ios/tools/build_ipa.sh: pack line missing after the MAPS assignment')
        t = t.replace(old3, '''if [ -n "${DM_FULL_ROMFS:-}" ]; then
  t "pack full IPA with RomFS (already-compressed game files stored, everything else deflated)"
else
  t "pack slim IPA without RomFS (game files come from Documents/romfs; DM_FULL_ROMFS=1 packs them)"
fi''', 1)
        return done('build_ipa.sh slim + entitlements', t)
    # older upstream: the RomFS mapping was its own statement right before the pack line
    old4 = '''MAPS+=("$P/romfs=$ROMFS")
t "pack IPA (already-compressed game files stored, everything else deflated)"'''
    if old4 not in t: fail('dm/ios/tools/build_ipa.sh: pack anchor missing (neither the folded '
                           'MAPS assignment nor the separate RomFS line is present)')
    return done('build_ipa.sh slim + entitlements', t.replace(old4, '''# DM_FULL_ROMFS=1 packs the RomFS into the package (upstream behaviour; only sensible for small
# games — a full package of a large game is many GB and iOS refuses to install it with
# PackageExtractionFailed). The default is the slim package: the app reads the game files from
# Documents/romfs (a directory tree, or a ROMFSARC archive: romfs.idx + romfs.NN shards).
if [ -n "${DM_FULL_ROMFS:-}" ]; then
  MAPS+=("$P/romfs=$ROMFS")
  t "pack full IPA with RomFS (already-compressed game files stored, everything else deflated)"
else
  t "pack slim IPA without RomFS (game files come from Documents/romfs; DM_FULL_ROMFS=1 packs them)"
fi''', 1))

def patch_nsp(t):
    P = []
    P.append(('work-dir arg', '--work-dir', [(
        "    ap.add_argument('--env-file', help='environment switches for a diagnostic build (one KEY=VALUE per line)')",
        "    ap.add_argument('--work-dir', help='where the build folder goes (default: <NSP folder>/<title ID>_build); the extracted files, shader cache and temporary files all live there')\n"
        "    ap.add_argument('--env-file', help='environment switches for a diagnostic build (one KEY=VALUE per line)')")]))
    # Two separate one-line anchors: upstream inserts lines between them whenever it adds a check
    # (ffcd910 put a mac_pack check right above `work`), which broke the old three-line anchor and
    # stopped the daily sync for two days (issue #13). `work` and that out_ipa line each appear once.
    # `out_ipa` reads `work`, so `work` must be assigned first: patched separately they once landed
    # in the wrong order (PR #15 — an UnboundLocalError on every build without -o). One replacement
    # per shape of upstream's block keeps them in order:
    #   - before upstream #278: one out_ipa line, the two checks, then `work`;
    #   - after #278: out_ipa under `if want_ios:` (8 spaces) plus an out_app block, `work` still last.
    # Each shape is ONE (old, new) pair rather than a delete plus an insert: a two-step edit that
    # removes `work` and puts it back leaves the tree without the assignment if the second step ever
    # stops matching.
    work_line = "    work = Path(os.path.expanduser(args.work_dir)).resolve() if args.work_dir else nsp_dir / f'{g.id}_build'   # the folder holding the NSPs by default; --work-dir puts the build elsewhere"
    oipa_old = "out_ipa = Path(os.path.expanduser(args.out or OUT_IPA)).resolve() if (args.out or OUT_IPA) else nsp_dir / g.get('ipa')   # by default in the NSP folder, next to the NSPs and <id>_build/"
    oipa_new = "out_ipa = Path(os.path.expanduser(args.out or OUT_IPA)).resolve() if (args.out or OUT_IPA) else work.parent / g.get('ipa')   # by default next to the NSPs and <id>_build/"
    shadowed_line = "    if shadowed and not (args.out or OUT_IPA): out_ipa = out_ipa.with_name(out_ipa.stem + '-provisional.ipa')   # never over the IPA of the game's real profile"
    macpack_line = "    if mac_pack and (args.out or OUT_IPA) and out_ipa.suffix != '.app': raise BuildError(f'the Mac app (-o / OUT_IPA) must end in .app: {out_ipa}')"
    if "\n    out_ipa = None\n    if want_ios:\n" in t:   # upstream #278's shape
        P.append(('work-dir paths', 'args.work_dir', [(
            "    out_ipa = None\n    if want_ios:\n        " + oipa_old + "\n",
            work_line + "\n    out_ipa = None\n    if want_ios:\n        " + oipa_new + "\n")]))
    else:
        P.append(('work-dir paths', 'args.work_dir', [(
            "    " + oipa_old + "\n" + shadowed_line + "\n" + macpack_line + "\n    work = nsp_dir / f'{g.id}_build'",
            work_line + "\n"
            "    # out_ipa reads `work`, so `work` must be assigned first (upstream's order had them the other\n"
            "    # way round; the --work-dir patch moved `work` up here and this line had to follow it).\n"
            "    " + oipa_new + "\n" + shadowed_line + "\n" + macpack_line)]))
    P.append(('DOTNET_ROOT', 'list-runtimes', [(
        "    env.setdefault('USER', getpass.getuser())   # xcodegen fails without USER (Couldn't find current username)",
        """    env.setdefault('USER', getpass.getuser())   # xcodegen fails without USER (Couldn't find current username)
    # nx_extract is a framework-dependent app: without DOTNET_ROOT the host refuses to start it even
    # when dotnet is on PATH (~/.dotnet is not a registered install location). The root is parsed from
    # `dotnet --list-runtimes`, not assumed to be the binary's directory: Homebrew's
    # /opt/homebrew/bin/dotnet is a symlink into Cellar and its runtime lives in …/libexec, so dirname
    # gave …/bin and the apphost failed to find its runtime. Also into os.environ: some steps run
    # without env= and inherit this process's environment.
    if not env.get('DOTNET_ROOT') and (dn := shutil.which('dotnet', path=env['PATH'])):
        root = None
        try:
            out = subprocess.run([dn, '--list-runtimes'], capture_output=True, text=True, timeout=60).stdout
            for ln in out.splitlines():
                if not ln.startswith('Microsoft.NETCore.App'): continue
                p = ln[ln.rfind('[') + 1:ln.rfind(']')] if '[' in ln else ''
                if p.endswith('/shared/Microsoft.NETCore.App'): root = p[:-len('/shared/Microsoft.NETCore.App')]
                elif p: root = p
                break
        except Exception: pass
        env['DOTNET_ROOT'] = os.environ['DOTNET_ROOT'] = root or str(Path(dn).resolve().parent)
    # The shader translator (dmsh_xlate_bin/dmsh_xlate) is a framework-dependent apphost like
    # nx_extract, and a set-but-wrong DOTNET_ROOT is worse than none: the apphost stops searching
    # once the variable is there, so a stale root from the environment (or the fallback above, wrong
    # for Homebrew's symlink layout) kills the shader step while `dotnet` on PATH keeps working —
    # nx_extract builds fine, then dmsh_build refuses with \"'xlate_source: lock … / actual' empty\".
    # Verify by actually running the translator; when it answers nothing, try candidate roots and
    # keep the first that works. With none verified the variable is dropped, so the apphost at least
    # searches its default locations.
    xlate_bin = repo / 'dm/gfx/tools/dmsh_xlate_bin/dmsh_xlate'
    def xlate_answers(root):
        e = dict(env)
        if root: e['DOTNET_ROOT'] = str(root)
        else: e.pop('DOTNET_ROOT', None)
        try:
            r = subprocess.run([str(xlate_bin), '--source-sha'], capture_output=True, text=True, env=e, timeout=120)
            return r.returncode == 0 and bool(r.stdout.strip())
        except Exception:
            return False
    if not xlate_answers(env.get('DOTNET_ROOT')):
        cands = []
        if dn := shutil.which('dotnet', path=env['PATH']):
            real = Path(dn).resolve()
            for base in (real.parent.parent, real.parent.parent.parent):
                cands += [base / 'libexec', base]   # Homebrew's bin/../libexec; share/dotnet and aliases
        cands += [Path.home() / '.dotnet', Path('/usr/local/share/dotnet'), Path('/opt/homebrew/opt/dotnet/libexec')]
        if hit := next((c for c in cands if c.is_dir() and xlate_answers(c)), None):
            env['DOTNET_ROOT'] = os.environ['DOTNET_ROOT'] = str(hit)
            say(f'DOTNET_ROOT: {hit} (verified — the shader translator runs)')
        elif env.get('DOTNET_ROOT'):
            del env['DOTNET_ROOT']; os.environ.pop('DOTNET_ROOT', None)
            say('DOTNET_ROOT: removed (none verified — the apphost searches its default locations)')""")]))
    P.append(('pwd import', 'import ast, collections, ctypes, getpass, pwd', [(
        "import ast, collections, ctypes, getpass, hashlib, importlib.util, json, os, re, shlex, shutil, struct, subprocess, sys, tempfile, threading, time, types",
        "import ast, collections, ctypes, getpass, pwd, hashlib, importlib.util, json, os, re, shlex, shutil, struct, subprocess, sys, tempfile, threading, time, types")]))
    # HOME can be missing or empty under a GUI launch (issue #10): .NET's first-run configure then
    # builds a mutex path from "" and dies with NuGet-Migrations + lstat("") before reading a project.
    P.append(('HOME fallback', 'DOTNET_CLI_HOME', [(
        "    env.setdefault('USER', getpass.getuser())   # xcodegen fails without USER (Couldn't find current username)",
        """    env.setdefault('USER', getpass.getuser())   # xcodegen fails without USER (Couldn't find current username)
    # HOME and DOTNET_CLI_HOME for every child: a GUI launch (LaunchServices) can hand the app a
    # process environment with HOME missing or empty, and .NET's first-run configure then builds a
    # mutex path from "" and dies with 'NuGet-Migrations' + lstat("") == ENOENT — before any project
    # is read (issue #10, where the app's own DOTNET_ROOT workaround did not help because the empty
    # HOME came from the launch, not from the app). Derive it from the password database when the
    # environment has nothing usable, so the pipeline is correct however it was started.
    if not env.get('HOME') or not os.path.isdir(env['HOME']):
        try: home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except Exception: home = Path.home()
        if home.is_dir():
            env['HOME'] = os.environ['HOME'] = str(home)
            say(f'HOME: {home} (taken from the password database; the environment had none)')
    env.setdefault('DOTNET_CLI_HOME', env['HOME'])   # .NET writes its first-run state here""")]))
    # Issue #10 also asked why the first-run experience is not simply skipped. It can be, with the
    # two knobs .NET still supports (SKIP_FIRST_TIME_EXPERIENCE was retired in .NET Core 3.0): the
    # banner and the telemetry call are noise for a build, and the telemetry endpoint can hang the
    # very first run on a mainland connection — which is how a "stuck at dotnet" report looks.
    P.append(('dotnet quiet', 'DOTNET_CLI_TELEMETRY_OPTOUT', [(
        "    env.setdefault('DOTNET_CLI_HOME', env['HOME'])   # .NET writes its first-run state here",
        """    env.setdefault('DOTNET_CLI_HOME', env['HOME'])   # .NET writes its first-run state here
    # No first-run banner, no telemetry: a build neither wants nor reads either, and the telemetry
    # call can hang the first run on a network that cannot reach Microsoft. setdefault, so a user
    # who wants telemetry on keeps it.
    env.setdefault('DOTNET_CLI_TELEMETRY_OPTOUT', '1')
    env.setdefault('DOTNET_NOLOGO', '1')""")]))
    P.append(('3.9 survey note', "listed = ', '.join('{} ({})'.format(n, mods[n]['kind'])", [(
        '''    if extra: notes.append(f'modules besides main / sdk: {", ".join(f"{n} ({mods[n]["kind"]})" for n in extra)}; the provisional profile loads {G["modules"][0]} (load order main, subsdk*, sdk)')''',
        '''    if extra:
        # Nested same-quote strings inside an f-string need Python 3.12 (PEP 701); this script has to
        # parse on the 3.9 that ships with the Xcode command line tools, so keep the inner
        # expressions out of the f-string.
        listed = ', '.join('{} ({})'.format(n, mods[n]['kind']) for n in extra)
        notes.append('modules besides main / sdk: ' + listed + '; the provisional profile loads '
                     + G['modules'][0] + ' (load order main, subsdk*, sdk)')''')]))
    P.append(('3.9 not-brought-up note', '_known = ", ".join("{} {}".format(v, b["display"])', [(
        '''        say(f'[RED] this {artifact} uses {pkg_ver} ({disp}), which game profile {reg.id} was not brought up on ''',
        '''        _known = ", ".join("{} {}".format(v, b["display"]) for v, b in reg.builds().items())
        say(f'[RED] this {artifact} uses {pkg_ver} ({disp}), which game profile {reg.id} was not brought up on '''),
        ('''({", ".join(f"{v} {b["display"]}" for v, b in reg.builds().items())})''',
        '''({_known})''')]))
    P.append(('3.9 derived note', '_reg = ", ".join("{} {}".format(v, b["display"]) for v, b in builds.items())', [(
        '''        if pkg_ver not in builds: derived = f'{pkg_ver} is not a version game profile {g.id} registers ({", ".join(f"{v} {b["display"]}" for v, b in builds.items())})\'''',
        '''        if pkg_ver not in builds:
            _reg = ", ".join("{} {}".format(v, b["display"]) for v, b in builds.items())
            derived = f'{pkg_ver} is not a version game profile {g.id} registers ({_reg})\'''')]))
    # An external work folder (an exFAT USB drive, the report's own setup) can report entries
    # during shutil.rmtree's walk that vanish by their unlink — FileNotFoundError mid-delete — and
    # the partially deleted folder then breaks the copytree right after. "Make this path gone" is
    # the contract, so ENOENT passes and everything else gets one retry.
    P.append(('rmtree exfat', '_rmtree_enoent', [(
        """def rmtree(p):
    if p.is_symlink() or p.is_file(): p.unlink()
    elif p.exists(): shutil.rmtree(p)""",
        """def _rmtree_enoent(func, p, exc):
    if isinstance(exc[1], FileNotFoundError): return   # already gone: the goal is achieved
    time.sleep(0.2); func(p)                           # one retry: exFAT directory-cache races clear


def rmtree(p):
    if p.is_symlink() or p.is_file(): p.unlink()
    elif p.exists(): shutil.rmtree(p, onerror=_rmtree_enoent)
    if p.exists(): shutil.rmtree(p, onerror=_rmtree_enoent)   # the caller needs it gone (copytree follows)""")]))
    # A shader-chain bump (an upstream translator update) silently invalidates every cached
    # library: the row stayed green, the build "worked", and hours of Metal compile looked like a
    # mystery re-run (the report: "why is it building shaders again, the artifacts are on the
    # drive"). Say it loudly, with a marker both front ends turn into a user-facing notice.
    P.append(('chain-change note', "[NOTE] chain-changed", [(
        """    chain = chain_of(repo)
    out = sh / 'cache' if mac else ios""",
        """    chain = chain_of(repo)
    stale = sorted(d.name for d in (sh / 'cache').glob('b-*') if d.name != f'b-{chain}' and any(d.glob('*_b.metallib'))
                   ) + sorted(d.name for d in ios.glob('b-*') if d.name != f'b-{chain}' and any(d.glob('*_b.metallib')))
    if stale: say(f'[NOTE] chain-changed: {", ".join(stale)} -> b-{chain}: the upstream update changed the shader '
                  'translator, so the cached libraries cannot be reused and this build rebuilds them with it '
                  '(once per update; later builds reuse as before)')
    out = sh / 'cache' if mac else ios""")]))
    # Xcode 15 and later download the iOS platform separately: xcodebuild and the Metal compiler can
    # both be present with no iOS SDK, and the failure then surfaced deep inside xcodebuild. Naming
    # the SDK makes the error actionable (found while auditing environment assumptions, 2026-10-26).
    P.append(('iOS SDK check', "the iOS SDK is not installed", [(
        """    gone = [f'{t} ({how})' for t, how in tools.items() if not shutil.which(t, path=env['PATH'])]
    if gone: raise BuildError('tools not found: ' + ', '.join(gone))
""",
        """    gone = [f'{t} ({how})' for t, how in tools.items() if not shutil.which(t, path=env['PATH'])]
    if gone: raise BuildError('tools not found: ' + ', '.join(gone))
    if not mac:
        # Xcode 15 and later ship the iOS platform as a separate download: xcodebuild and the Metal
        # compiler can both be there while the iOS SDK is not, and the failure then surfaces deep in
        # xcodebuild. Check the SDK itself so the message names the right command.
        sdk = subprocess.run(['xcrun', '--sdk', 'iphoneos', '--show-sdk-version'], capture_output=True, text=True, env=env)
        if sdk.returncode:
            raise BuildError('the iOS SDK is not installed (Xcode 15 and later download the platform separately): run xcodebuild -downloadPlatform iOS, or pick it in Xcode → Settings → Components')
""")]))
    for name, marker, pairs in P:
        if marker in t:
            skip(f'NSP script {name} (already patched)'); continue
        hit = True
        for old, new in pairs:
            if old not in t:
                if name.startswith('3.9'):   # upstream may fix these themselves; a miss is not an error
                    hit = False; skip(f'NSP script {name} (upstream no longer has the pattern)'); break
                fail(f'scripts/build_ipa_from_nsp.py: patch "{name}" anchor missing')
            t = t.replace(old, new, 1)
        if hit: done(f'NSP script {name}')
    return t

def patch_shader_split(t):
    # Upstream fixed the same 3.9 f-string themselves (d4249e5: a label variable); either fix ends it.
    if 'label = re.sub(r"^(\\.\\./)+", "", os.path.normpath(pk.mount + name))' in t: return skip('shader_split.py (upstream fixed it)')
    if 'a backslash cannot sit inside an f-string expression' in t: return skip('shader_split.py (already patched)')
    old = '''        ue_inline_shaders(d, f'/{rel}#{re.sub(r"^(\\.\\./)+", "", os.path.normpath(pk.mount + name))}', out)'''
    if old not in t: fail('dm/gfx/tools/shader_split.py: ue_inline_shaders anchor missing')
    # The same shape as upstream's own fix (d4249e5), so the marker holds whichever of the two applied
    new = '''        label = re.sub(r"^(\\.\\./)+", "", os.path.normpath(pk.mount + name))   # a backslash cannot sit inside an f-string expression before 3.12
        ue_inline_shaders(d, f'/{rel}#{label}', out)'''
    return done('shader_split.py 3.9', t.replace(old, new, 1))

NVM = 'dm/gfx/nvn/nvn_mtl.m'
def patch_nvn_mtl(t):
    # The fork's fix for the iOS save-load freeze (NX2Apple issue #4): an upload batch's commit can sit
    # recorded in the open replay block (rec_commit_upload) while the submit thread sleeps on a slot or
    # on g_up_pending — nothing else hands the block over mid-submission, so the submission can never
    # end and the wait never resolves. Upstream fixed the up_begin half (Little Nightmares II) and then
    # ported our mtl_finish_gpu half with their own comment (b87a0cc, plus their up_flush self-publish
    # for waiters on any thread); the markers accept either comment, ours or theirs.
    P = [
        # BC decode on GPUs without BC support (profile tex_bc_cpu; nvn_bcdec.c is an OURS file)
        ('bc cpu globals', ['g_bc_cpu;'], [(
            'static id<MTLDevice> g_dev;',
            '''static id<MTLDevice> g_dev;
static int g_bc_gpu = 1;   // the device advertises BC texture compression (set where the device is made)
static int g_bc_cpu;       // decode BC on the CPU: no GPU support and the profile allows it''')]),
        ('bc cpu include', ['nvn_bcdec.h'], [(
            '#include "../tex/nvn_fmt_mtl.h"',
            '#include "../tex/nvn_fmt_mtl.h"\n#include "../tex/nvn_bcdec.h"   // BC on the CPU for GPUs without BC support (profile tex_bc_cpu)')]),
        ('bc cpu detect', ['supportsBCTextureCompression'], [(
            '''    g_dev = MTLCreateSystemDefaultDevice();
    if (!g_dev) MDIE("no Metal device");
    if (!g_dev.hasUnifiedMemory) MDIE("needs a unified-memory device (zero-copy memory pools)");''',
            '''    g_dev = MTLCreateSystemDefaultDevice();
    if (!g_dev) MDIE("no Metal device");
    if (!g_dev.hasUnifiedMemory) MDIE("needs a unified-memory device (zero-copy memory pools)");
    g_bc_gpu = [g_dev supportsBCTextureCompression];
    g_bc_cpu = !g_bc_gpu && DM_GAME_TEX_BC_CPU;
    if (g_bc_cpu) MLOG("BC textures: this GPU has no BC support, decoding them on the CPU (profile tex_bc_cpu)");''')]),
        # BC decode on the CPU when the GPU has no BC support (profile tex_bc_cpu): the texture is
        # created uncompressed, and the upload expands the blocks before the blit.
        ('bc cpu tex_desc', ['nvn_bc_unpacked(t->d.format, &bc_unpacked'], [(
            '    uint32_t pf = 0; uint8_t swz[4];\n    if (!nvn_fmt_to_mtl(t->d.format, &pf, swz) || !pf) {',
            '    uint32_t pf = 0; uint8_t swz[4];\n    // A BC format on a GPU without BC support: create the texture in the uncompressed format the\n    // decoder produces, and let the upload path fill it through nvn_bc_decode (below). Without\n    // this the Metal format is a BC one the device cannot allocate, and every such texture is nil.\n    uint32_t bc_unpacked = 0; int bc_bpp = 0;\n    if (g_bc_cpu && nvn_bc_unpacked(t->d.format, &bc_unpacked, &bc_bpp)) pf = bc_unpacked;\n    if (!pf && (!nvn_fmt_to_mtl(t->d.format, &pf, swz) || !pf)) {')]),
        ('bc cpu upload', ['nvn_bc_decode(t->d.format'], [(
            '    up_begin();\n    id<MTLBuffer> stg = [g_dev newBufferWithLength:total ? total : 16 options:MTLResourceStorageModeShared];\n    res_add(stg); [g_up_keep addObject:stg];\n    size_t off = 0;\n    int zb = zblock_on() && t->d.format >= 0x79 && t->d.format <= 0x94; if (zb) { g_zb_ser = t->serial; g_zb_n = (int)MIN(d.mipmapLevelCount, 16u); memset(g_zb, 0, sizeof g_zb); memset(g_zb_tot, 0, sizeof g_zb_tot); }\n    for (unsigned l = 0; l < d.mipmapLevelCount; l++) {\n        uint32_t w = MAX(t->d.width >> l, 1u), h = MAX(t->d.height >> l, 1u), z = t->d.target == 2 ? MAX(t->d.depth >> l, 1u) : 1;\n        size_t pitch = (size_t)((w + bw - 1) / bw) * bpb, rows = (h + bh - 1) / bh;\n        for (unsigned y = 0; y < layers; y++) {\n            if (nvn_deswizzle(&t->d, l, y, src, (uint8_t *)stg.contents + off, pitch)) { RED_ONCE(9, "deswizzle failed format %#x target %d", t->d.format, t->d.target); break; }\n            if (zb && l < 16) { const uint64_t *q = (const uint64_t *)((const uint8_t *)stg.contents + off); size_t nb = pitch * rows * z / 16;\n                for (size_t i = 0; i < nb; i++) g_zb[l] += !(q[2 * i] | q[2 * i + 1]);\n                g_zb_tot[l] += (uint32_t)nb; }\n            up_blit_add(stg, off, pitch, pitch * rows, MTLSizeMake(w, h, z), mt, y, l);\n',
            '    // A BC texture on a GPU without BC support: tex_desc made the Metal texture RGBA8 (or R8/RG8,\n    // RGBA16F for BC6H), so the staging slot is per-pixel, not per-block, and the deswizzled block\n    // data is expanded through nvn_bc_decode before the blit. bc_cpu stays 0 on Macs and A19+,\n    // where the compressed path below is used unchanged.\n    uint32_t bc_pf = 0; int bc_bpp = 0;\n    int bc_cpu = g_bc_cpu && nvn_bc_unpacked(t->d.format, &bc_pf, &bc_bpp);\n    size_t total_bc = 0;\n    if (bc_cpu) {\n        total_bc = 0;\n        for (unsigned l = 0; l < d.mipmapLevelCount; l++) {\n            uint32_t w = MAX(t->d.width >> l, 1u), h = MAX(t->d.height >> l, 1u), z = t->d.target == 2 ? MAX(t->d.depth >> l, 1u) : 1;\n            total_bc += (size_t)w * h * z * layers * (size_t)bc_bpp;\n        }\n    }\n    up_begin();\n    id<MTLBuffer> stg = [g_dev newBufferWithLength:(bc_cpu ? total_bc : total) ? (bc_cpu ? total_bc : total) : 16 options:MTLResourceStorageModeShared];\n    res_add(stg); [g_up_keep addObject:stg];\n    size_t off = 0;\n    int zb = zblock_on() && t->d.format >= 0x79 && t->d.format <= 0x94; if (zb) { g_zb_ser = t->serial; g_zb_n = (int)MIN(d.mipmapLevelCount, 16u); memset(g_zb, 0, sizeof g_zb); memset(g_zb_tot, 0, sizeof g_zb_tot); }\n    // CPU-side scratch for the compressed block data when bc_cpu (one level-slice at a time)\n    uint8_t *bc_tmp = NULL; size_t bc_tmp_sz = 0;\n    for (unsigned l = 0; l < d.mipmapLevelCount; l++) {\n        uint32_t w = MAX(t->d.width >> l, 1u), h = MAX(t->d.height >> l, 1u), z = t->d.target == 2 ? MAX(t->d.depth >> l, 1u) : 1;\n        size_t pitch = (size_t)((w + bw - 1) / bw) * bpb, rows = (h + bh - 1) / bh;\n        for (unsigned y = 0; y < layers; y++) {\n            if (bc_cpu) {\n                size_t need = pitch * rows * z;\n                if (need > bc_tmp_sz) { free(bc_tmp); bc_tmp = malloc(need); bc_tmp_sz = need; if (!bc_tmp) MDIE("BC decode scratch of %zu bytes failed", need); }\n                if (nvn_deswizzle(&t->d, l, y, src, bc_tmp, pitch)) { RED_ONCE(9, "deswizzle failed format %#x target %d", t->d.format, t->d.target); break; }\n                size_t dst_pitch = (size_t)w * bc_bpp;\n                for (uint32_t zz = 0; zz < z; zz++)\n                    nvn_bc_decode(t->d.format, bc_tmp + (size_t)zz * pitch * rows, pitch, w, h,\n                                  (uint8_t *)stg.contents + off + (size_t)zz * dst_pitch * h, dst_pitch);\n                up_blit_add(stg, off, dst_pitch, dst_pitch * h, MTLSizeMake(w, h, z), mt, y, l);\n                off += dst_pitch * h * z;\n                continue;\n            }\n            if (nvn_deswizzle(&t->d, l, y, src, (uint8_t *)stg.contents + off, pitch)) { RED_ONCE(9, "deswizzle failed format %#x target %d", t->d.format, t->d.target); break; }\n            if (zb && l < 16) { const uint64_t *q = (const uint64_t *)((const uint8_t *)stg.contents + off); size_t nb = pitch * rows * z / 16;\n                for (size_t i = 0; i < nb; i++) g_zb[l] += !(q[2 * i] | q[2 * i + 1]);\n                g_zb_tot[l] += (uint32_t)nb; }\n            up_blit_add(stg, off, pitch, pitch * rows, MTLSizeMake(w, h, z), mt, y, l);\n')]),
        ('bc cpu free', ['free(bc_tmp);\n    free(part);'], [(
            "    free(part);\n    up_flush();\n    return mt;",
            "    free(bc_tmp);\n    free(part);\n    up_flush();\n    return mt;")]),
        ('up_begin publish', ['the iOS save-load freeze of NX2Apple issue #4', "Little Nightmares II's first render passes"], [(
            '''    unsigned slot = g_up_seq++ % 3;
    up_wait_zero(&g_up_alloc_busy[slot]);''',
            '''    unsigned slot = g_up_seq++ % 3;
    if (atomic_load(&g_up_alloc_busy[slot])) rec_publish();   // The commit of the batch that still holds this slot sits in the open replay block (rec_commit_upload). Hand the block to the encode thread before sleeping on the slot: a submit thread that opens a 4th batch within one submission would otherwise wait forever — nothing else hands the block over mid-submission, and the submission cannot end while up_begin blocks (the iOS save-load freeze of NX2Apple issue #4)
    up_wait_zero(&g_up_alloc_busy[slot]);''')]),
        ('finish publish', ['if (on_sub_thread()) rec_publish();'], [(
            '''    if (g_fence) mtl_wait(g_fence, UINT64_MAX);
    up_wait_zero(&g_up_pending);''',
            '''    if (g_fence) mtl_wait(g_fence, UINT64_MAX);
    if (on_sub_thread()) rec_publish();   // Pending upload commits sit in the open replay block: hand it over before waiting on g_up_pending, or a loader's QueueFinish mid-submission (Three Houses-style per-resource waits) sleeps forever. Other threads (suspend, diagnostics) cannot publish the submit thread's block; their waits resolve once the submission ends
    up_wait_zero(&g_up_pending);''')]),
    ]
    for name, markers, pairs in P:
        if any(m in t for m in markers):
            skip(f'nvn_mtl.m {name} (already patched)'); continue
        for old, new in pairs:
            if old not in t: fail(f'{NVM}: patch "{name}" anchor missing: {old[:70]!r}')
            t = t.replace(old, new, 1)
        done(f'nvn_mtl.m {name}')
    return t

MAIN_M_ROMFS = ('''static NSString *romfs_locate(void) {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSString *doc = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES)[0];
    NSString *img = [doc stringByAppendingPathComponent:@"romfs.img"];
    if (romfs_image_complete(img)) {
        // A folder copy from before images is now dead weight of up to many GB (often cut off partway). Deleting its 300,000 files here took longer than iOS allows an app to start
        // (Tears of the Kingdom: killed on a black screen), so it is moved aside at once and deleted in the background, leftovers of an interrupted delete included
        NSString *old = [doc stringByAppendingPathComponent:@"romfs"], *tmp = NSTemporaryDirectory();
        if ([fm fileExistsAtPath:old] && [fm moveItemAtPath:old toPath:[tmp stringByAppendingPathComponent:[@"romfs-old-" stringByAppendingString:NSUUID.UUID.UUIDString]] error:nil])
            fprintf(stderr, "boot: Documents/romfs moved aside for deletion (Documents/romfs.img replaces it)\\n");
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_BACKGROUND, 0), ^{
            NSFileManager *bfm = [NSFileManager new];
            for (NSString *n in [bfm contentsOfDirectoryAtPath:tmp error:nil]) if ([n hasPrefix:@"romfs-old-"]) [bfm removeItemAtPath:[tmp stringByAppendingPathComponent:n] error:nil];
        });
        return img;
    }
    NSString *bundledImage = [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"romfs.img"];
    NSString *documentFolder = [doc stringByAppendingPathComponent:@"romfs"];
    if ([fm fileExistsAtPath:[documentFolder stringByAppendingPathComponent:@DM_GAME_ROMFS_MARKER]]) return documentFolder;
    if (romfs_image_complete(bundledImage)) return bundledImage;
    for (NSString *d in @[[doc stringByAppendingPathComponent:@"romfs"], [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"romfs"]])
        if ([fm fileExistsAtPath:[d stringByAppendingPathComponent:@DM_GAME_ROMFS_MARKER]]) return d;   // only a directory containing the game profile\'s romfs_marker path counts (Mario: System/Resource, the resource size table)
    return nil;
}

''', '''// A folder holds this game\'s files if it has a ROMFSARC index or the profile\'s marker path.
// Shared by romfs_locate and the per-game layout below, so both answer the same question.
static BOOL romfs_dir_ok(NSString *d) {
    NSFileManager *fm = NSFileManager.defaultManager;
    // A ROMFSARC archive: <dir>/romfs.idx next to romfs.NN shards. The host opens it when DM_ROMFS
    // points at the base; the marker check does not apply (there are no directories to walk).
    if ([fm fileExistsAtPath:[d stringByAppendingPathComponent:@"romfs.idx"]]) return YES;
    // A plain directory tree: only one containing the game profile\'s romfs_marker path counts
    // (Mario: System/Resource, the resource size table). Runtime profile: the marker is a profile
    // value now; the compiled macro is only what dm_profile() falls back to without DM_PROFILE.
    return [fm fileExistsAtPath:[d stringByAppendingPathComponent:@(dm_profile()->romfs_marker)]];
}

// Where one game\'s data lives under the per-game layout: Documents/games/<title ID>/, holding
//   romfs.img   the whole RomFS as one image file (what scripts/push_romfs.sh sends), or
//   romfs/      a ROMFSARC archive or a plain tree, for copies made before images
//   msl/        this game\'s Metal shader libraries (optional; the app\'s own dm/ is used otherwise)
//   save/ temp/ this game\'s saves and scratch space
// The layout exists so several games can share one installed app and so the game data can sit on
// an external drive the Files app exposes (a folder copied there keeps this shape).
static NSString *game_dir_for(const char *title_id) {
    NSString *doc = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES)[0];
    return [[doc stringByAppendingPathComponent:@"games"] stringByAppendingPathComponent:@(title_id)];
}

static NSString *romfs_locate(void) {
    NSFileManager *fm = NSFileManager.defaultManager;
    NSString *doc = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES)[0];
    // Per-game first: Documents/games/<id>/romfs.img (one file, what push_romfs.sh sends) or
    // Documents/games/<id>/romfs/ — the launcher-style layout, and what lets a user keep several
    // games\' data side by side or on an external drive. The marker check inside romfs_dir_ok is
    // what keeps a stray folder from being taken for this game.
    NSString *gdir = game_dir_for(dm_profile()->id);
    NSString *gimg = [gdir stringByAppendingPathComponent:@"romfs.img"];
    if (romfs_image_complete(gimg)) return gimg;
    NSString *gfolder = [gdir stringByAppendingPathComponent:@"romfs"];
    if ([fm fileExistsAtPath:gfolder] && romfs_dir_ok(gfolder)) return gfolder;
    NSString *img = [doc stringByAppendingPathComponent:@"romfs.img"];
    if (romfs_image_complete(img)) {
        // A folder copy from before images is now dead weight of up to many GB (often cut off partway). Deleting its 300,000 files here took longer than iOS allows an app to start
        // (Tears of the Kingdom: killed on a black screen), so it is moved aside at once and deleted in the background, leftovers of an interrupted delete included
        NSString *old = [doc stringByAppendingPathComponent:@"romfs"], *tmp = NSTemporaryDirectory();
        if ([fm fileExistsAtPath:old] && [fm moveItemAtPath:old toPath:[tmp stringByAppendingPathComponent:[@"romfs-old-" stringByAppendingString:NSUUID.UUID.UUIDString]] error:nil])
            fprintf(stderr, "boot: Documents/romfs moved aside for deletion (Documents/romfs.img replaces it)\\n");
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_BACKGROUND, 0), ^{
            NSFileManager *bfm = [NSFileManager new];
            for (NSString *n in [bfm contentsOfDirectoryAtPath:tmp error:nil]) if ([n hasPrefix:@"romfs-old-"]) [bfm removeItemAtPath:[tmp stringByAppendingPathComponent:n] error:nil];
        });
        return img;
    }
    NSString *bundledImage = [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"romfs.img"];
    NSString *documentFolder = [doc stringByAppendingPathComponent:@"romfs"];
    if ([fm fileExistsAtPath:[documentFolder stringByAppendingPathComponent:@(dm_profile()->romfs_marker)]]) return documentFolder;
    if (romfs_image_complete(bundledImage)) return bundledImage;
    for (NSString *d in @[[doc stringByAppendingPathComponent:@"romfs"], [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"romfs"]])
        if (romfs_dir_ok(d)) return d;
    return nil;
}

''')


MAIN_M_ENV = ('    setenv("DM_ROMFS", romfs.fileSystemRepresentation, 0);\n    setenv("DM_SAVE", [doc stringByAppendingPathComponent:@"save"].fileSystemRepresentation, 0);\n    setenv("DM_TEMP", [doc stringByAppendingPathComponent:@"temp"].fileSystemRepresentation, 0);\n    NSString *dm = [res stringByAppendingPathComponent:@"dm"];\n    setenv("DM_NVN_SHMAP", [dm stringByAppendingPathComponent:@"shader_map.tsv"].fileSystemRepresentation, 0);\n    setenv("DM_MSL_DIR", [res stringByAppendingPathComponent:@"msl"].fileSystemRepresentation, 0);\n', '    setenv("DM_ROMFS", romfs.fileSystemRepresentation, 0);\n    // Saves and scratch space: Documents/games/<id>/{save,temp} when the per-game layout is in use\n    // (so two games in one app never share a save folder), else the old Documents/{save,temp}. An\n    // env file still wins — DM_SAVE/DM_TEMP set there are already in the environment and\n    // setenv(..., 0) leaves them alone.\n    NSString *gdir = game_dir_for(dm_profile()->id);\n    // Tie the data folder to where the game files actually came from: if romfs_locate fell back to\n    // Documents/romfs, the saves must stay beside it (a game folder without romfs is not in use).\n    BOOL per_game = [romfs hasPrefix:gdir];\n    NSString *save_dir = per_game ? [gdir stringByAppendingPathComponent:@"save"] : [doc stringByAppendingPathComponent:@"save"];\n    NSString *temp_dir = per_game ? [gdir stringByAppendingPathComponent:@"temp"] : [doc stringByAppendingPathComponent:@"temp"];\n    for (NSString *d in @[save_dir, temp_dir]) [fm createDirectoryAtPath:d withIntermediateDirectories:YES attributes:nil error:nil];\n    setenv("DM_SAVE", save_dir.fileSystemRepresentation, 0);\n    setenv("DM_TEMP", temp_dir.fileSystemRepresentation, 0);\n    if (per_game) fprintf(stderr, "boot: per-game data folder %s\\n", gdir.fileSystemRepresentation);\n    NSString *dm = [res stringByAppendingPathComponent:@"dm"];\n    setenv("DM_NVN_SHMAP", [dm stringByAppendingPathComponent:@"shader_map.tsv"].fileSystemRepresentation, 0);\n    // Shader libraries: a game\'s own <gdir>/msl first (a per-game layout may carry its own), else\n    // the app\'s shared <App>/msl.\n    NSString *per_game_msl = [gdir stringByAppendingPathComponent:@"msl"];\n    NSString *msl = (per_game && [fm fileExistsAtPath:per_game_msl]) ? per_game_msl : [res stringByAppendingPathComponent:@"msl"];\n    setenv("DM_MSL_DIR", msl.fileSystemRepresentation, 0);\n')

def patch_main_m(t):
    """romfs_locate + the per-game data layout in dm/ios/Sources/main.m.

    Two replacements, both anchored on upstream text:
    - romfs_locate becomes romfs_dir_ok() + game_dir_for() + a locate that tries
      Documents/games/<id>/romfs first (the launcher-style layout, which is also what lets the
      game data sit in a folder the Files app exposes on an external drive).
    - the DM_ROMFS/DM_SAVE/DM_TEMP/DM_MSL_DIR block gains the per-game folders and the shader
      library fallback.
    A missing anchor is an error: these are the fork's only two changes here.
    """
    P = [
        # the runtime profile replaced the compiled header: hle_profile.h gives dm_profile(), which
        # the marker check below reads (merged with the stage-2 experiment, 21fbba1)
        ('runtime profile include',
         ('#include "game_profile.h"   // game profile constants (the RomFS marker path)',
          '#include "hle_profile.h"   // the runtime game profile (DM_PROFILE): the RomFS marker path comes from it')),
        ('romfs per-game', MAIN_M_ROMFS),
        ('per-game data dirs', MAIN_M_ENV),
    ]
    for name, (old, new) in P:
        if new in t:
            skip(f'main.m {name} (already patched)')
            continue
        if old not in t:
            fail(f'dm/ios/Sources/main.m: patch "{name}" anchor missing (upstream moved romfs_locate '
                 'or the environment block)')
        t = t.replace(old, new, 1)
        done(f'main.m {name}')
    return t

def patch_dm_h(t):
    """dm.h pulls in hle_profile.h, so every host source can call dm_profile().

    The runtime-profile work (merged from the stage-2 experiment, 21fbba1) turned the compiled
    game_profile.h constants into values read from a profile at start, and dm_profile() is how
    every source reaches them — but only hle_svc.c and hle_profile.c included the header. It
    compiled then because the other callers were older upstream revisions; after a sync rewrote
    them, loader.c, hle_fs.c, hle_fpslock.c and five more failed with "call to undeclared function
    dm_profile". Including it from dm.h (which they all include) is what keeps the accessor in
    scope without touching each file, and this entry is what keeps it in scope across a sync.
    """
    if 'hle_profile.h' in t: return skip('dm.h hle_profile.h (already patched)')
    old = '#include "game_profile.h"   // constants of the current game profile'
    if old not in t: fail('dm/host/dm.h: game_profile.h include anchor missing')
    line = next(ln for ln in t.splitlines() if ln.startswith(old))
    return done('dm.h hle_profile.h', t.replace(line, line + '\n#include "hle_profile.h"'
        '   // the runtime profile (DM_PROFILE): every constant above can also come from it;'
        ' dm_profile() is the accessor', 1))


ROMFS_TEST_STUB = ('typedef struct { const char *id, *romfs_marker; } fixture_profile;   /* the fork reads both from the runtime profile */\n'
                   'static const fixture_profile *dm_profile(void) { static const fixture_profile p = {"0100000000000000", "marker"}; return &p; }\n')

def patch_test_romfs_startup(t):
    """The test compiles main.m's RomFS locator alone, with a stub for each name it needs. The fork's
    locator reads the game's id and romfs_marker through dm_profile() (the runtime profile, patch_main_m),
    which upstream's stubs do not provide: clang stopped on "call to undeclared function dm_profile".
    The fixture gets a dm_profile() with the marker the test already uses."""
    if 'fixture_profile' in t: return skip('test_romfs_startup dm_profile stub (already patched)')
    old = "#define DM_GAME_ROMFS_MARKER \"marker\"\\n'"
    if old not in t: fail('dm/ios/tests/test_romfs_startup.py: the fixture preamble anchor is missing'); return t
    return done('test_romfs_startup dm_profile stub', t.replace(old, old + "\n                          " + repr(ROMFS_TEST_STUB), 1))

def patch_test_profile_pack(t):
    """Upstream packs the RomFS into the IPA; the fork's default is the slim package (patch_build_ipa:
    DM_FULL_ROMFS=1 packs it), so the test checks that the RomFS stays out instead."""
    if 'slim package' in t: return skip('test_profile_pack slim IPA (already patched)')
    old = "                    self.assertEqual(archive.read('Payload/DmIOS.app/romfs/marker'), b'romfs')"
    if old not in t: fail('dm/ios/tests/test_profile_pack.py: the RomFS assertion anchor is missing'); return t
    new = ("                    # the fork's slim package: the RomFS stays out of the IPA (DM_FULL_ROMFS=1 packs it)\n"
           "                    self.assertFalse(any(n.startswith('Payload/DmIOS.app/romfs') for n in names))")
    return done('test_profile_pack slim IPA', t.replace(old, new, 1))


# The switches only the fork's code reads, with their rows in docs/env_switches.tsv (name, class, meaning)
FORK_SWITCHES = {
    'DM_FULL_ROMFS': ('switch', '`=1` makes `build_ipa.sh` pack the RomFS (and DLC) into the IPA, as upstream does; the fork\'s default is the slim package, whose game data the app reads from `Documents/games/<title ID>/`'),
    'DM_PROFILE': ('path', '`=<file>`: the runtime game profile (`game.py profile`) the host reads at start (`hle_profile.c`); without it the compiled `game_profile.h` values apply'),
}

def patch_env_switches(t):
    """docs/env_switches.tsv is upstream's table of every switch the code reads, and test_env_switches.py
    fails when a switch the code reads has no row. The fork's own switches (FORK_SWITCHES) lose their rows
    whenever upstream rewrites the table; they go back in name order."""
    rows = t.rstrip('\n').split('\n')
    have = {r.split('\t', 1)[0] for r in rows}
    missing = [n for n in FORK_SWITCHES if n not in have]
    if not missing: return skip('env_switches.tsv fork rows (already patched)')
    head, body = rows[0], rows[1:] + [f'{n}\t{c}\t{m}' for n, (c, m) in FORK_SWITCHES.items() if n in missing]
    return done('env_switches.tsv fork rows: ' + ' '.join(missing), '\n'.join([head] + sorted(body, key=lambda r: r.split('\t', 1)[0])) + '\n')


def patch_hades2(t):
    """The fork's Hades II profile carries what we verified end to end — the update (v1,
    1.0.137848) registered and packed, the measured NVN texture slots (35: slot 34 lies past the
    translator's built-in 32) and the widescreen investigation notes — and upstream's is v0-only.
    The fork's full file ships beside it as game.fork.tsv and is restored wholesale when the
    sync loses the marker; if upstream reworks the profile, merge by hand."""
    if 'build	v1	1.0.137848' in t: return skip('Hades II profile (already patched)')
    fork = (REPO / 'dm/games/0100A00019DE0000/game.fork.tsv').read_text(encoding='utf-8')
    say('  … restoring the fork\'s Hades II profile (update v1, measured slots, notes)')
    return done('Hades II profile v1', fork)

# One marker per carrier: the string that exists only when the fork's patch is in place.
MARKERS = {
    'dm/host/hle_fs.c': 'romfsarc',
    'dm/host/Makefile': 'romfs_arc.c',
    'dm/host/tests/Makefile': 'romfs_arc.c',
    'dm/ios/tools/build_ipa.sh': 'DM_FULL_ROMFS',
    'scripts/build_ipa_from_nsp.py': '--work-dir',
    'dm/gfx/tools/shader_split.py': "ue_inline_shaders(d, f'/{rel}#{label}', out)",
    'dm/ios/Sources/main.m': 'romfs_dir_ok',
    'dm/gfx/nvn/nvn_mtl.m': 'if (on_sub_thread()) rec_publish();',
    'dm/games/0100A00019DE0000/game.tsv': 'build	v1	1.0.137848',
    'dm/host/dm.h': 'hle_profile.h',
    'dm/ios/tests/test_romfs_startup.py': 'fixture_profile',
    'dm/ios/tests/test_profile_pack.py': 'slim package',
    'docs/env_switches.tsv': 'DM_FULL_ROMFS\t',
}
PATCHERS = {
    'dm/host/hle_fs.c': patch_hle_fs,
    'dm/host/Makefile': patch_makefile,
    'dm/host/tests/Makefile': patch_tests_makefile,
    'dm/ios/tools/build_ipa.sh': patch_build_ipa,
    'scripts/build_ipa_from_nsp.py': patch_nsp,
    'dm/gfx/tools/shader_split.py': patch_shader_split,
    'dm/ios/Sources/main.m': patch_main_m,
    'dm/gfx/nvn/nvn_mtl.m': patch_nvn_mtl,
    'dm/games/0100A00019DE0000/game.tsv': patch_hades2,
    'dm/host/dm.h': patch_dm_h,
    'dm/ios/tests/test_romfs_startup.py': patch_test_romfs_startup,
    'dm/ios/tests/test_profile_pack.py': patch_test_profile_pack,
    'docs/env_switches.tsv': patch_env_switches,
}

report, problems = [], []
class SyncProblem(Exception): pass
def say(s): report.append(s); print(s, flush=True)
def fail(s):
    problems.append(s); say('[FAIL] ' + s)
    raise SyncProblem(s)
def skip(name): say(f'  = {name}')
def done(name, t=None):
    say(f'  + {name}')
    return t

def upstream_mode(rev, path):
    """The file's git mode upstream ('100755' for an executable), None when it is not there."""
    out = git('ls-tree', rev, '--', path).split()
    return out[0] if out else None

def take_from_upstream(rev, path, dest):
    data = blob(rev, path)
    if data is None: fail(f'{path}: not in upstream {rev}'); return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    # The mode too: written as bytes, an executable came out 0644, and the fork's spirv-cross binary, its build.sh and six
    # scripts lost their x bit (test_spirv_cross_pin: Permission denied running the committed spirv-cross)
    dest.chmod(0o755 if upstream_mode(rev, path) == '100755' else 0o644)

def verify(skip_build):
    say('--- verify ---')
    py = '/usr/bin/python3' if os.path.exists('/usr/bin/python3') else sys.executable
    bad = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in ('.git', 'third_party', '__pycache__', '.build', 'node_modules')]
        for f in files:
            if not f.endswith('.py'): continue
            p = Path(root) / f
            r = subprocess.run([py, '-c', 'import ast,sys\ntry: ast.parse(open(sys.argv[1],encoding="utf-8",errors="replace").read())\nexcept SyntaxError as e: print(f"{e.lineno}: {e.msg}")', str(p)], capture_output=True, text=True)
            if r.stdout.strip(): bad.append(f'{p.relative_to(REPO)}:{r.stdout.strip()}')
    if bad:
        for b in bad: fail(f'Python 3.9 syntax: {b}')
    else: say(f'  + every .py parses on the Python 3.9 floor')
    r = subprocess.run(['bash', '-n', 'dm/ios/tools/build_ipa.sh'], cwd=REPO, capture_output=True, text=True)
    if r.returncode: fail(f'build_ipa.sh: bash -n failed: {r.stderr.strip()}')
    else: say('  + build_ipa.sh: bash -n OK')
    # archive reader: compile and run against a small generated archive
    with tempfile.TemporaryDirectory() as td:
        src, arc = Path(td) / 'src', Path(td) / 'romfs'
        (src / 'sub').mkdir(parents=True)
        (src / 'a.txt').write_bytes(b'hello'); (src / 'sub' / 'b.txt').write_bytes(b'world')
        r = subprocess.run([sys.executable, 'dm/tools/romfs_arc.py', 'pack', str(src), str(arc), '--shard-mib', '1'], cwd=REPO, capture_output=True, text=True)
        if r.returncode: fail(f'archive pack failed: {r.stderr.strip()[:200]}'); return
        exe = Path(td) / 't_arc'
        r = subprocess.run(['clang', '-O1', '-I', 'dm/host', '-o', str(exe), 'dm/host/tests/t_romfs_arc.c', 'dm/host/romfs_arc.c'], cwd=REPO, capture_output=True, text=True)
        if r.returncode: fail(f'archive test compile failed: {r.stderr.strip()[:400]}'); return
        r = subprocess.run([str(exe), str(arc)], capture_output=True, text=True)
        if r.returncode or 'all checks passed' not in r.stdout: fail(f'archive test failed: {r.stdout.strip()[:200]} {r.stderr.strip()[:200]}')
        else: say('  + archive reader test passed')
    if skip_build: say('  = host build skipped (--skip-build)'); return
    games = sorted(p.name for p in (REPO / 'dm/games').iterdir() if p.name.startswith('0100'))
    game = '01006F8002326000' if '01006F8002326000' in games else games[0]
    say(f'  … building the macOS host for {game} (a few minutes)')
    r = subprocess.run(['make', '-C', 'dm/host', f'GAME={game}', 'dmhost', '-j4'], cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        tail = '\n'.join((r.stdout + r.stderr).splitlines()[-25:])
        fail(f'host build failed:\n{tail}')
    else: say(f'  + host build OK ({game})')
    # Host unit tests: the fs tests #include ../hle_fs.c and link romfs_arc.c, so a fork change that
    # the test rules do not know about breaks them at link time and they stop running silently
    # (PR #12 — they had been unbuildable). Cheap (seconds) and catches that class here.
    say(f'  … host unit tests for {game}')
    r = subprocess.run(['make', '-C', 'dm/host/tests', f'GAME={game}', 'run', '-j4'], cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        tail = '\n'.join((r.stdout + r.stderr).splitlines()[-25:])
        fail(f'host unit tests failed:\n{tail}')
    else: say('  + host unit tests OK')
    check_patcher_consistency()


def check_patcher_consistency():
    """Every carrier's patched form must equal the file in the tree, code line by code line.

    A fork change made to a carrier's source but not to its patcher entry is invisible today (the
    marker is present, so the patcher skips) and reappears the next time upstream touches that file
    — PR #11 was exactly that: GetEntryType had been fixed to arc_rel in the source while the patcher
    still inserted ROMFSARC_SYNTH_REL. Comparing the patcher's output against the working tree makes
    the drift show up on the sync that introduces it rather than months later.

    Comments and blank lines are ignored: a comment reworded in the tree is not drift, and demanding
    a matching comment edit in the patcher would train people to ignore this check.
    """
    say('  … patcher consistency (patched upstream file vs the tree, comments ignored)')
    def code(text):
        out = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s or s.startswith(('#', '//', '/*', '*', '<!--')):
                continue
            # strip a trailing comment too: `code;  // why` is the same statement however the why
            # is worded, and a reworded trailing comment must not read as drift
            cut = ln.find('  //')
            if cut < 0:
                cut = ln.find('  #')
            if cut > 0:
                ln = ln[:cut]
            out.append(ln.rstrip())
        return out
    drift = []
    for path, patcher in PATCHERS.items():
        src = REPO / path
        if not src.exists():
            continue
        upstream = subprocess.run(['git', 'show', f'upstream/main:{path}'], cwd=REPO,
                                  capture_output=True, text=True)
        if upstream.returncode:
            continue                      # a fork-only file has no upstream form to compare
        try:
            out = patcher(upstream.stdout)
        except SyncProblem:
            continue                      # an anchor miss is reported by apply_changes already
        if out is None:
            out = upstream.stdout
        if code(out) != code(src.read_text(encoding='utf-8', errors='replace')):
            drift.append(path)
    if drift:
        for d in drift:
            say(f'  ! {d}: the patcher and the tree disagree')
        fail('patcher drift: ' + ', '.join(drift) +
             ' — the source was changed without its patcher entry (or the other way round); '
             'a future sync would silently revert the change')
    else:
        say(f'  + {len(PATCHERS)} patchers agree with the tree')

def apply_changes(a, changes):
    """Take every changed file from upstream, then re-apply the fork's patches to the carriers."""
    touched = 0
    for kind, path in changes:
        dest = REPO / path
        if kind == 'D':
            if path in OURS: fail(f'upstream deleted a fork file: {path}')
            if dest.exists(): dest.unlink(); say(f'  - {path}'); touched += 1
            continue
        if path in OURS: fail(f'upstream writes over a fork file: {path} (resolve by hand)')
        take_from_upstream(a.new, path, dest); touched += 1
        if path in PATCHED:
            r = PATCHERS[path](dest.read_text(encoding='utf-8', errors='replace'))
            if r is not None and r != dest.read_text(encoding='utf-8', errors='replace'):
                dest.write_text(r, encoding='utf-8')
    say(f'{touched} files taken from upstream')
    # Second pass over every carrier, including ones upstream did not change this time: a fork
    # patch can be lost without the file ever appearing in the diff (a wholesale tree replace,
    # a bad merge), so presence is asserted, not assumed.
    for path, patcher in PATCHERS.items():
        p = REPO / path
        if not p.exists(): fail(f'{path}: missing before patching')
        t = p.read_text(encoding='utf-8', errors='replace')
        r = patcher(t)
        if r is not None and r != t: p.write_text(r, encoding='utf-8')
        now = p.read_text(encoding='utf-8', errors='replace')
        if MARKERS[path] not in now:
            fail(f'{path}: fork patch marker missing after sync: {MARKERS[path]!r}')


def game_rows():
    """Every profile in the tree, as the README table wants it: name, title ID, update, status.

    Read from `dm/games/<title ID>/game.tsv`, which is the only authority: the table used to be
    maintained by hand, so a sync that brought in new profiles (three did, with 467 commits) left
    the README claiming 72 games while the tree had 75.

    `build` holds the update's version and version number (`v131072 1.0.2 main=…`); `package` is
    the version the profile needs. A profile with no build line needs no update at all.
    """
    rows = []
    for d in sorted((REPO / 'dm/games').iterdir()):
        if not d.is_dir() or not d.name.startswith('0100'):
            continue
        tsv = d / 'game.tsv'
        if not tsv.is_file():
            continue
        fields: dict[str, str] = {}
        for ln in tsv.read_text(encoding='utf-8', errors='replace').splitlines():
            if not ln or ln.startswith('#') or '\t' not in ln:
                continue
            k, v = ln.split('\t', 1)
            fields.setdefault(k.strip(), v.strip())
        name = fields.get('name')
        if not name:
            continue
        # `build` repeats once per version this profile knows (v0 the base game, then each
        # update), and `package` names the one the profile is built for. Reading the first build
        # line reported the base version for every game, which is how "无（v0）" became "1.0.0 (v0)"
        # and Super Mario Bros. Wonder's 1.2.1 became 1.0.0.
        builds = [ln.split('\t', 1)[1].strip()
                  for ln in tsv.read_text(encoding='utf-8', errors='replace').splitlines()
                  if ln.startswith('build\t')]
        want = fields.get('package', 'v0').strip() or 'v0'
        chosen = next((b for b in builds if b.split() and b.split()[0] == want), None)
        parts = (chosen or '').split()
        if len(parts) >= 2 and parts[0] != 'v0':
            update = f'{parts[1]} ({parts[0]})'
        else:
            update = '无（v0）'
        rows.append((d.name, name, update))
    return rows


#: The status a profile gets until someone writes something better. Kept as one string so the
#: wording matches what the table already uses for unverified titles.
STATUS_UNVERIFIED = 'Profile present; not verified on a phone'
STATUS_UPSTREAM = 'Playable on an iPhone (upstream)'

#: Rows whose status is this fork's own testing (build → install → play here). Matched by title ID
#: so the text can change without the sync dropping the claim.
VERIFIED_HERE = {
    '0100000000010000': '**Verified here** — builds, installs and runs on an iPhone; '
                        '304,243 files into 127 archive shards, byte-verified',
    '0100F2C0115B6000': '**Verified here** — builds, installs and runs on an iPhone; '
                        '33-file archive',
    '01006F8002326000': '**Verified here** — builds, installs and runs smoothly on an iPhone; '
                        '103,645 files into 72 archive shards',
}


def refresh_game_table():
    """Rebuild the README's game table from `dm/games/`, keeping every hand-written status.

    Two things must not be lost when this rewrites the table:
    - a status someone wrote by hand (a verification, a caveat about a controller or touch
      controls). The existing row's status column is carried over verbatim; only rows that did
      not exist before get a default.
    - the sort order, which is by title ID and is what makes the table readable.

    The row count in the prose ("72 game profiles") and the "N verified" line are updated too,
    because those are the numbers a reader trusts and the ones that went stale.
    """
    readme_path = REPO / 'README.md'
    text = readme_path.read_text(encoding='utf-8')
    rows = game_rows()
    if not rows:
        say('  ! no game profiles found; the table is left alone')
        return

    # existing statuses, by title ID
    have: dict[str, str] = {}
    for ln in text.splitlines():
        m = re.match(r'\|\s*([^|]+?)\s*\|\s*`(0100[0-9A-F]{12})`\s*\|\s*([^|]*?)\s*\|\s*(.+?)\s*\|\s*$', ln)
        if m:
            have[m.group(2)] = m.group(4)

    lines = ['| Game | Title ID | Update required | Status |', '|---|---|---|---|']
    added = 0
    for tid, name, update in rows:
        if tid in have:
            status = have[tid]
        elif tid in VERIFIED_HERE:
            status = VERIFIED_HERE[tid]
        else:
            status = STATUS_UNVERIFIED
            added += 1
        lines.append(f'| {name} | `{tid}` | {update} | {status} |')
    table = '\n'.join(lines)

    # replace the table in place: from its header row to the last row that starts with "| "
    start = text.find('| Game | Title ID | Update required | Status |')
    if start < 0:
        say('  ! game table not found in README.md; profiles are in the tree but not listed')
        return
    end = start
    for ln in text[start:].splitlines(keepends=True):
        if ln.startswith('| ') or ln.startswith('|---'):
            end += len(ln)
        else:
            break
    new_text = text[:start] + table + '\n' + text[end:]

    # the counts in the prose and in the table's own caption
    n = len(rows)
    verified = sum(1 for ln in table.splitlines() if '**Verified here**' in ln)
    # Only the number changes; whatever emoji or wording precedes it is left exactly as written
    # (the feature table uses 🧩, the hero caption uses 🎯). A lookahead keeps the match to the
    # digits, so no part of the surrounding text is rewritten.
    new_text = re.sub(r'\d+(?= game profiles\*\*)', str(n), new_text)
    new_text = re.sub(r'\*\*\d+ verified end to end on this fork\*\*',
                      f'**{verified} verified end to end on this fork**', new_text)

    if new_text != text:
        readme_path.write_text(new_text, encoding='utf-8')
        say(f'  README table: {n} profiles' + (f' ({added} new)' if added else '') +
            f', {verified} verified here')
    else:
        say(f'  README table: {n} profiles, unchanged')


def refresh_game_table_zh():
    """Add rows for profiles the Chinese README's table is missing, and nothing else.

    The English table is rebuilt because its game names come straight from `game.tsv`. The
    Chinese one cannot be: its names are translated by hand ("超级马力欧 奥德赛" for "Super Mario
    Odyssey"), and so are its statuses. So this only appends rows for title IDs that are absent,
    using the English name as a placeholder — visible, and better than a game that silently is
    not listed — with the Chinese wording for "unverified" so it does not stand out as English
    text in a Chinese table. A translator replaces the name later; the row is never regenerated
    once it exists, so the translation is safe.

    Sorting is by title ID, like the English table, so the insert position is computed rather
    than appended at the end.
    """
    path = REPO / 'README.zh-CN.md'
    if not path.is_file():
        return
    text = path.read_text(encoding='utf-8')
    header = '| 游戏 | Title ID | 需要的更新包 | 状态 |'
    start = text.find(header)
    if start < 0:
        say('  ! Chinese game table not found; skipped')
        return
    # the rows of the table, in order
    block_start = start
    block_end = start
    for ln in text[start:].splitlines(keepends=True):
        if ln.startswith('| ') or ln.startswith('|---'):
            block_end += len(ln)
        else:
            break
    block = text[block_start:block_end]
    present = set(re.findall(r'`(0100[0-9A-F]{12})`', block))

    missing = [r for r in game_rows() if r[0] not in present]
    if not missing:
        say(f'  README.zh-CN table: complete ({len(present)} profiles)')
        return

    lines = block.rstrip('\n').splitlines()
    for tid, name, update in missing:
        row = f'| {name} | `{tid}` | {update} | 有 profile，未在手机上验证 |'
        # insert before the first row whose title ID sorts after this one
        pos = len(lines)
        for i, ln in enumerate(lines):
            m = re.search(r'`(0100[0-9A-F]{12})`', ln)
            if m and m.group(1) > tid:
                pos = i
                break
        lines.insert(pos, row)
    new_block = '\n'.join(lines) + '\n'
    new_text = text[:block_start] + new_block + text[block_end:]
    n = len(game_rows())
    new_text = re.sub(r'\*\*(?:[^ *] )?(\d+)( game profiles\*\*)',
                      lambda m: f'**{n}{m.group(2)}', new_text)
    path.write_text(new_text, encoding='utf-8')
    say(f'  README.zh-CN table: +{len(missing)} row(s) '
        f'({", ".join(t for t, _, _ in missing)}) — names are English until translated')


def refresh_badges():
    """Keep docs/badges/*.json in step with the tree, so the README badges need no hand edit.

    The game count is the number of title-ID profile folders (dm/games/0100…), which is what the
    README means by "N game profiles". The verified count is the rows marked verified in the
    README's own table, so the source of truth stays the README text rather than a second list."""
    gdir = REPO / 'dm/games'
    # Counted the same way the table is built (game_rows: a folder needs a game.tsv with a name),
    # not by directory name: upstream has documentation folders under dm/games/ that carry no
    # profile, and the badge said 80 while the table listed 79.
    games = len(game_rows())
    readme = (REPO / 'README.md').read_text(encoding='utf-8')
    verified = sum(1 for ln in readme.splitlines()
                   if ln.startswith('| ') and ('**Verified here**' in ln or 'Verified —' in ln))
    for name, label, value, color in [('games', 'games', games, 'blue'),
                                      ('verified', 'verified on an iPhone', verified, 'success')]:
        p = REPO / 'docs/badges' / f'{name}.json'
        old = p.read_text(encoding='utf-8') if p.exists() else ''
        new = json.dumps({'schemaVersion': 1, 'label': label, 'message': str(value), 'color': color},
                         indent=2) + '\n'
        if old != new:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new, encoding='utf-8')
            say(f'  badge {name}: {value}')
    say(f'badges: {games} games, {verified} verified')
    # The README banners carry the same two numbers, so they are redrawn here: both scripts read
    # them from the tree (repo_numbers()), so this only runs when the tree changed.
    for script in ('frontend/screenshots/gen_hero.py', 'frontend/screenshots/gen_hero_en.py'):
        sp = REPO / script
        if not sp.exists():
            continue
        r = subprocess.run([sys.executable, str(sp)], capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            say(f'  banner: {Path(script).name} redrawn')
        else:
            say(f'  ! banner {Path(script).name} failed: {(r.stderr or "").strip()[:160]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--old', required=True, help='upstream ref the fork is currently at')
    ap.add_argument('--new', required=True, help='upstream ref to sync to')
    ap.add_argument('--skip-build', action='store_true')
    a = ap.parse_args()

    say(f'sync {a.old[:7]} → {a.new[:7]}')
    changes = upstream_changes(a.old, a.new)
    say(f'{len(changes)} upstream file changes under {" ".join(VENDORED)}')
    try:
        apply_changes(a, changes)
        verify(a.skip_build)
        refresh_game_table()
        refresh_game_table_zh()
        refresh_badges()
    except SyncProblem: pass
    if problems:
        say(f'\nSYNC FAILED: {len(problems)} problem(s) — nothing committed; the working tree is left as the script wrote it, `git checkout -- .` restores it')
        sys.exit(1)
    say('\nSYNC OK — commit and push from the caller')


if __name__ == '__main__':
    main()
