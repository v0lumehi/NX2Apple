import XCTest
import SwiftUI
import Combine
@testable import NSPortBuilder

final class LibraryTests: XCTestCase {
    private var temporary: URL!
    private let titleID = "0100535012974000"

    override func setUpWithError() throws {
        temporary = FileManager.default.temporaryDirectory.appendingPathComponent("nxnative-tests-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try FileManager.default.removeItem(at: temporary)
    }

    private func makeApp(_ titleID: String, name: String = "Hades") throws -> URL {
        let app = temporary.appendingPathComponent(name + ".app")
        let executables = app.appendingPathComponent("Contents/MacOS")
        try FileManager.default.createDirectory(at: executables, withIntermediateDirectories: true)
        for name in ["nxnative", "dmhost"] {
            try FileManager.default.createSymbolicLink(at: executables.appendingPathComponent(name), withDestinationURL: URL(fileURLWithPath: "/usr/bin/true"))
        }
        let info: [String: Any] = ["DMTitleID": titleID, "CFBundleDisplayName": name, "CFBundleExecutable": "nxnative"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: app.appendingPathComponent("Contents/Info.plist"))
        return app
    }

    func testReadyRequiresMatchingGameAndCompleteExecutables() throws {
        let app = try makeApp(titleID)
        var game = LibraryGame(titleID: titleID, name: "Hades", appPath: app.path)
        XCTAssertTrue(game.isReady)
        game.titleID = "01007EF00011E000"
        XCTAssertFalse(game.isReady)
        game.titleID = titleID
        try FileManager.default.removeItem(at: app.appendingPathComponent("Contents/MacOS/dmhost"))
        XCTAssertFalse(game.isReady)
        XCTAssertThrowsError(try MacGameBundle.read(app))
    }

    func testRejectsUnrelatedApplicationsAndExecutableTraversal() throws {
        let app = try makeApp("not-a-title-id")
        XCTAssertThrowsError(try MacGameBundle.read(app))
        let info: [String: Any] = ["DMTitleID": titleID, "CFBundleExecutable": "../../../../usr/bin/true"]
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: app.appendingPathComponent("Contents/Info.plist"))
        XCTAssertThrowsError(try MacGameBundle.read(app))
    }

    func testCollectionPersistsPathsAndRemovalLeavesFilesAlone() throws {
        let app = try makeApp(titleID)
        let file = temporary.appendingPathComponent("state/library.json")
        let library = GameLibrary(file: file)
        let id = try library.addApp(app)
        let image = temporary.appendingPathComponent("my-picture.png")
        try Data("not game data".utf8).write(to: image)
        library.setArtwork(image.path, for: id)
        let restored = GameLibrary(file: file)
        XCTAssertEqual(restored.games.count, 1)
        XCTAssertEqual(restored.games.first?.artworkPath, image.path)
        XCTAssertEqual(restored.games.first?.appPath, app.path)
        XCTAssertTrue(restored.games[0].isReady)
        restored.remove(id)
        XCTAssertTrue(GameLibrary(file: file).games.isEmpty)
        XCTAssertTrue(FileManager.default.fileExists(atPath: app.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: image.path))
    }

    func testSuccessfulBuildMustMatchTheLibraryEntry() throws {
        let app = try makeApp(titleID)
        let file = temporary.appendingPathComponent("library.json")
        let game = LibraryGame(titleID: titleID, name: "Hades")
        try JSONEncoder().encode([game]).write(to: file)
        let library = GameLibrary(file: file)
        library.recordCompletedApp(path: temporary.appendingPathComponent("missing.app").path, for: game.id)
        XCTAssertFalse(library.games[0].isReady)
        let wrongGame = try makeApp("01007EF00011E000", name: "Other")
        library.recordCompletedApp(path: wrongGame.path, for: game.id)
        XCTAssertFalse(library.games[0].isReady)
        library.recordCompletedApp(path: app.path, for: game.id)
        XCTAssertTrue(library.games[0].isReady)
        try FileManager.default.removeItem(at: app)
        XCTAssertFalse(library.games[0].isReady)
    }

    func testRebuildingSameGameKeepsOneEntryAndCustomArtwork() throws {
        let app = try makeApp(titleID)
        let library = GameLibrary(file: temporary.appendingPathComponent("library.json"))
        let first = try library.addApp(app)
        library.setArtwork("/some/external/artwork.jpg", for: first)
        XCTAssertEqual(try library.addApp(app), first)
        XCTAssertEqual(library.games.count, 1)
        XCTAssertEqual(library.games[0].artworkPath, "/some/external/artwork.jpg")
    }

    private func writeSettings(_ app: URL) throws {
        let dm = app.appendingPathComponent("Contents/Resources/dm")
        try FileManager.default.createDirectory(at: dm, withIntermediateDirectories: true)
        try Data("""
        {"game": "\(titleID)", "settings": [
         {"env": "DM_FPS_TARGET", "kind": "choice", "section": "Game", "label": "Frame rate", "note": "", "default": "30", "options": [["30", "30 fps"], ["60", "60 fps"]]},
         {"env": "DM_RENDER_SCALE", "kind": "range", "section": "Game", "label": "Render scale", "note": "", "default": "1.0", "min": 0.5, "max": 1.0, "step": 0.05},
         {"env": "DM_FPS_HOLD", "kind": "toggle", "section": "Performance", "label": "Hold frames when late", "note": "", "default": "on", "on": "on", "off": "off"}]}
        """.utf8).write(to: dm.appendingPathComponent("launcher.json"))
    }

    func testLaunchOptionsKeepOnlyChoicesAndReachTheEnvironment() throws {
        let app = try makeApp(titleID)
        XCTAssertNil(LaunchSetting.read(app: app.path))   // packed before the settings were
        try writeSettings(app)
        let settings = try XCTUnwrap(LaunchSetting.read(app: app.path))
        XCTAssertEqual(settings.map(\.env), ["DM_FPS_TARGET", "DM_RENDER_SCALE", "DM_FPS_HOLD"])
        let file = temporary.appendingPathComponent("library.json")
        let library = GameLibrary(file: file)
        let id = try library.addApp(app)
        XCTAssertEqual(library.games[0].launchEnvironment(settings), ["DM_FPS_TARGET": "30", "DM_RENDER_SCALE": "1.0", "DM_FPS_HOLD": "on"])
        library.setLaunchOption(settings[0], "60", for: id)
        library.setLaunchOption(settings[1], "1.00", for: id)   // the default, as a slider writes it: not kept
        library.setLaunchOption(settings[2], "off", for: id)
        XCTAssertEqual(GameLibrary(file: file).games[0].launchOptions, ["DM_FPS_TARGET": "60", "DM_FPS_HOLD": "off"])
        XCTAssertEqual(library.games[0].launchEnvironment(settings), ["DM_FPS_TARGET": "60", "DM_RENDER_SCALE": "1.0", "DM_FPS_HOLD": "off"])
        library.setLaunchOption(settings[0], "30", for: id)
        XCTAssertNil(library.games[0].launchOptions["DM_FPS_TARGET"])
        library.resetLaunchOptions(for: id)
        XCTAssertTrue(library.games[0].launchOptions.isEmpty)
    }

    func testLaunchOptionTheBuildNoLongerOffersFallsBackToItsDefault() throws {
        let app = try makeApp(titleID)
        try writeSettings(app)
        let settings = try XCTUnwrap(LaunchSetting.read(app: app.path))
        var game = LibraryGame(titleID: titleID, name: "Hades", appPath: app.path)
        game.launchOptions = ["DM_FPS_TARGET": "120", "DM_RENDER_SCALE": "2", "DM_FPS_HOLD": "maybe"]
        XCTAssertEqual(game.launchEnvironment(settings), ["DM_FPS_TARGET": "30", "DM_RENDER_SCALE": "1.0", "DM_FPS_HOLD": "on"])
    }

    func testBannersComeFromTheTitleListAndTakeTheHero() async throws {
        let images = temporary.appendingPathComponent("cdn")
        try FileManager.default.createDirectory(at: images, withIntermediateDirectories: true)
        let banner = images.appendingPathComponent("banner.jpg")
        let picture = NSImage(size: NSSize(width: 64, height: 36), flipped: false) { rect in NSColor.orange.setFill(); rect.fill(); return true }
        let tiff = try XCTUnwrap(picture.tiffRepresentation)
        try XCTUnwrap(NSBitmapImageRep(data: tiff)?.representation(using: .jpeg, properties: [:])).write(to: banner)
        try Data("not an image".utf8).write(to: images.appendingPathComponent("broken.jpg"))
        let list = temporary.appendingPathComponent("US.en.json")
        try Data("""
        {"1": {"id": "\(titleID.lowercased())", "bannerUrl": "\(banner.absoluteString)"},
         "2": {"id": "01007EF00011E000", "bannerUrl": "\(images.appendingPathComponent("broken.jpg").absoluteString)"},
         "3": {"id": "0100000000010000", "bannerUrl": "https://example.com/elsewhere.jpg"},
         "4": {"id": null, "bannerUrl": null}}
        """.utf8).write(to: list)
        let folder = temporary.appendingPathComponent("artwork")
        let store = ArtworkStore(directory: folder, titleList: list, allowsFileURLs: true)
        let added = await store.fetchMissing([titleID, "01007EF00011E000", "0100000000010000", "not-a-title"])
        XCTAssertEqual(added, [titleID])   // the broken image and the host off Nintendo's CDN are skipped
        XCTAssertNotNil(ArtworkStore.cachedBanner(titleID, in: folder))
        XCTAssertNil(ArtworkStore.cachedBanner("01007EF00011E000", in: folder))
        XCTAssertTrue(FileManager.default.fileExists(atPath: folder.appendingPathComponent("index.json").path))
        let again = await store.fetchMissing([titleID])
        XCTAssertEqual(again, [])   // already there: no download

        let app = try makeApp(titleID)
        var game = LibraryGame(titleID: titleID, name: "Hades", appPath: app.path)
        XCTAssertEqual(GameLibrary.artwork(for: game, repo: temporary.path, banners: folder)?.size, NSSize(width: 64, height: 36))
        let own = temporary.appendingPathComponent("own.png")
        let small = NSImage(size: NSSize(width: 8, height: 8), flipped: false) { rect in NSColor.blue.setFill(); rect.fill(); return true }
        try XCTUnwrap(NSBitmapImageRep(data: XCTUnwrap(small.tiffRepresentation))?.representation(using: .png, properties: [:])).write(to: own)
        game.artworkPath = own.path
        XCTAssertEqual(GameLibrary.artwork(for: game, repo: temporary.path, banners: folder)?.size, NSSize(width: 8, height: 8))   // the player's own image first
    }

    /// Stop ends the build's whole process group: the script and the tools it started (here two sleeps), even
    /// though the app itself ignores SIGTERM.
    func testStopEndsTheWholeProcessGroup() throws {
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else { throw XCTSkip("System Python is required") }
        let previous = signal(SIGTERM, SIG_IGN)   // as the running app does
        defer { signal(SIGTERM, previous) }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "sleep 60 & sleep 60 & wait"]
        BuildModel.inOwnGroup(p, python: "/usr/bin/python3")
        try p.run()
        func members() -> Int {
            let ps = Process(), out = Pipe()
            ps.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
            ps.arguments = ["-g", String(p.processIdentifier)]
            ps.standardOutput = out
            try? ps.run(); ps.waitUntilExit()
            return String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self).split(separator: "\n").count
        }
        var tries = 0
        while members() < 3 && tries < 50 { usleep(100_000); tries += 1 }
        XCTAssertEqual(members(), 3)   // the shell and its two children, in the leader's group
        let stopped = Date()
        BuildModel.endGroup(p.processIdentifier)
        p.waitUntilExit()
        tries = 0
        while members() > 0 && tries < 30 { usleep(100_000); tries += 1 }
        XCTAssertEqual(members(), 0)
        // Ended by the SIGTERM, well before the SIGKILL 5 s later. The shell may die of it or, when its children went
        // first, return from its wait and exit 0: either way the whole group is gone.
        XCTAssertLessThan(Date().timeIntervalSince(stopped), 3)
    }

    func testProgressDetailReadsTheScriptsCounts() {
        XCTAssertEqual(BuildModel.progressDetail(["step": "splitting", "done": 1203, "total": 19592, "unit": "shaders", "eta": 130]),
                       "splitting · " + NumberFormatter.localizedString(from: 1203, number: .decimal) + " / "
                       + NumberFormatter.localizedString(from: 19592, number: .decimal) + " shaders")
        XCTAssertEqual(BuildModel.progressDetail(["done": 2301, "unit": "files"]), NumberFormatter.localizedString(from: 2301, number: .decimal) + " files")
        XCTAssertTrue(BuildModel.progressDetail(["done": 1_000_000_000, "total": 3_000_000_000, "unit": "B", "est": true]).contains(" / ~"))
        XCTAssertEqual(BuildModel.progressDetail(["step": "booting the game"]), "booting the game")
    }

    /// The stage's clock runs on while the count stands still, and the script's estimate counts down from when it came, then goes.
    func testStageClockCountsOnAndTheEstimateRunsOut() {
        let t0 = Date()
        let stage = Stage(id: 1, name: "shader split", started: t0, eta: 71, etaAt: t0.addingTimeInterval(10))
        XCTAssertEqual(stage.clock(at: t0.addingTimeInterval(42), left: "%@ left"), "0:42 · " + BuildModel.duration(39) + " left")
        XCTAssertEqual(stage.clock(at: t0.addingTimeInterval(125), left: "%@ left"), "2:05")   // the estimate ran out: no stale "1 min 11 s left"
        XCTAssertEqual(stage.clock(at: t0.addingTimeInterval(3725), left: "%@ left"), "1:02:05")
    }

    /// Stop ends the build and leaves no stage behind (a spinner and a percentage of a build that no longer runs).
    @MainActor func testStoppedBuildResetsItsProgress() throws {
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else { throw XCTSkip("System Python is required for the process fixture") }
        let saved = ["repoPath", "buildTarget", "buildOutputDirectory"].map { ($0, UserDefaults.standard.object(forKey: $0)) }
        defer { for (key, value) in saved { UserDefaults.standard.set(value, forKey: key) } }
        let model = BuildModel()
        model.repoPath = temporary.path
        model.target = "mac-app"
        model.outDir = temporary.path
        model.pythonPath = "/usr/bin/python3"
        model.dotnetPath = "/usr/bin/true"
        model.depsChecked = true
        model.deps = [BuildModel.Dep(id: "test", name: "test", hint: "", present: true, installCmd: [])]
        let marker = temporary.appendingPathComponent("dm/tools/nx_extract/Program.cs")
        try FileManager.default.createDirectory(at: marker.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: marker)
        let script = temporary.appendingPathComponent("scripts/build_ipa_from_nsp.py")
        try FileManager.default.createDirectory(at: script.deletingLastPathComponent(), withIntermediateDirectories: true)
        try """
        import json, time
        print(json.dumps({'ev': 'stage', 'k': 1, 'n': 2, 'name': 'shader split', 'step': 'splitting'}), flush=True)
        print(json.dumps({'ev': 'progress', 'k': 1, 'name': 'shader split', 'step': 'splitting', 'done': 10, 'total': 40, 'unit': 'files', 'frac': 0.25}), flush=True)
        time.sleep(60)
        """.write(to: script, atomically: true, encoding: .utf8)
        let base = temporary.appendingPathComponent("Test [\(titleID)].nsp"), keys = temporary.appendingPathComponent("prod.keys")
        try Data([0]).write(to: base); try Data().write(to: keys)
        model.slots[0].paths = [base.path]; model.slots[3].paths = [keys.path]
        let running = expectation(description: "The stage is reported")
        let started = model.$stages.filter { $0.first?.percent == 25 }.prefix(1).sink { _ in running.fulfill() }
        model.startBuild()
        wait(for: [running], timeout: 10)
        let stopped = expectation(description: "The build ends")
        let ended = model.$running.dropFirst().filter { !$0 }.prefix(1).sink { _ in stopped.fulfill() }
        model.stop()
        wait(for: [stopped], timeout: 10)
        withExtendedLifetime((started, ended)) {}
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertTrue(model.stages.isEmpty)
        XCTAssertFalse(model.done)
        XCTAssertEqual(model.doneNote, model.t("msg.stopped"))
    }

    func testCorruptLibraryIsReportedWithoutOverwritingIt() throws {
        let file = temporary.appendingPathComponent("library.json")
        let data = Data("broken JSON".utf8)
        try data.write(to: file)
        let library = GameLibrary(file: file)
        XCTAssertNotNil(library.error)
        XCTAssertEqual(try Data(contentsOf: file), data)
    }

    func testFolderDiscoveryPrunesGameContentsAndRemembersLocation() throws {
        let app = try makeApp(titleID)
        let nested = temporary.appendingPathComponent("My games/Hades/mac")
        try FileManager.default.createDirectory(at: nested, withIntermediateDirectories: true)
        let moved = nested.appendingPathComponent(app.lastPathComponent)
        try FileManager.default.moveItem(at: app, to: moved)
        let hidden = temporary.appendingPathComponent("RomFS/ShouldNotScan.app")
        try FileManager.default.createDirectory(at: hidden.deletingLastPathComponent(), withIntermediateDirectories: true)
        try FileManager.default.copyItem(at: moved, to: hidden)
        XCTAssertEqual(GameLibrary.builtApps(in: temporary).map { $0.resolvingSymlinksInPath().path }, [moved.resolvingSymlinksInPath().path])
        XCTAssertTrue(GameLibrary.builtApps(in: temporary.appendingPathComponent("unmounted-volume")).isEmpty)
        let state = temporary.appendingPathComponent("library.json")
        let library = GameLibrary(file: state)
        let finished = expectation(description: "Folder scan completes")
        let token = library.$scanning.dropFirst().filter { !$0 }.sink { _ in finished.fulfill() }
        library.setFolder(temporary)
        wait(for: [finished], timeout: 3)
        withExtendedLifetime(token) {}
        XCTAssertEqual(GameLibrary(file: state).folder, temporary.path)
        XCTAssertEqual(library.games.map(\.titleID), [titleID])
    }

    func testReadOnlyScanOfConfiguredFolder() throws {
        guard let path = ProcessInfo.processInfo.environment["NX_LAUNCHER_SCAN_FOLDER"] else { throw XCTSkip("Set NX_LAUNCHER_SCAN_FOLDER for a read-only scan of real builds") }
        let apps = GameLibrary.builtApps(in: URL(fileURLWithPath: path))
        XCTAssertFalse(apps.isEmpty)
        for url in apps {
            let metadata = try MacGameBundle.read(url)
            print("Discovered Mac build: \(metadata.name) (\(metadata.titleID))")
        }
    }

    @MainActor func testCompletedBuildAppearsAutomaticallyInCollection() throws {
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else { throw XCTSkip("System Python is required for the process fixture") }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let saved = ["repoPath", "buildTarget", "buildOutputDirectory"].map { ($0, UserDefaults.standard.object(forKey: $0)) }
        defer { for (key, value) in saved { UserDefaults.standard.set(value, forKey: key) } }
        let model = BuildModel()
        model.repoPath = temporary.path
        model.target = "mac-app"
        model.outDir = temporary.path
        model.pythonPath = "/usr/bin/python3"
        model.dotnetPath = "/usr/bin/true"
        model.depsChecked = true
        model.deps = [BuildModel.Dep(id: "test", name: "test", hint: "", present: true, installCmd: [])]
        let marker = temporary.appendingPathComponent("dm/tools/nx_extract/Program.cs")
        try FileManager.default.createDirectory(at: marker.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: marker)
        let script = temporary.appendingPathComponent("scripts/build_ipa_from_nsp.py")
        try FileManager.default.createDirectory(at: script.deletingLastPathComponent(), withIntermediateDirectories: true)
        // This is a process contract fixture, not a game build or game data.
        try """
        import sys, pathlib, plistlib, json
        assert sys.argv[sys.argv.index('--progress') + 1] == 'json'
        def ev(**e): print(json.dumps(e), flush=True)
        ev(ev='stage', k=1, n=1, name='Mac app', step='starting')
        ev(ev='progress', k=1, name='Mac app', step='game files', done=1200, total=4800, unit='files', est=False, note='', frac=0.25, eta=90, elapsed=3)
        out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
        (out / 'Contents/MacOS').mkdir(parents=True)
        for name in ['nxnative', 'dmhost']:
            (out / 'Contents/MacOS' / name).symlink_to('/usr/bin/true')
        with (out / 'Contents/Info.plist').open('wb') as f:
            plistlib.dump({'DMTitleID': '\(titleID)', 'CFBundleName': 'Test build', 'CFBundleExecutable': 'nxnative'}, f)
        ev(ev='stage_end', k=1, name='Mac app', ok=True, summary='Test build.app', elapsed=4)
        ev(ev='say', msg='[12:00:00] done')
        """.write(to: script, atomically: true, encoding: .utf8)
        let base = temporary.appendingPathComponent("Test [\(titleID)].nsp")
        let keys = temporary.appendingPathComponent("prod.keys")
        try Data([0]).write(to: base)
        try Data().write(to: keys)
        model.slots[0].paths = [base.path]
        model.slots[3].paths = [keys.path]
        let library = GameLibrary(file: temporary.appendingPathComponent("library.json"))
        let host = NSHostingView(rootView: ContentView(model: model, library: library, initialPage: .builds, performStartupChecks: false))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 800), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        window.orderFront(nil)
        defer { window.orderOut(nil) }
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        let completed = expectation(description: "Successful app is added without an import")
        let token = library.$games.filter { $0.contains(where: \.isReady) }.prefix(1).sink { _ in completed.fulfill() }
        XCTAssertTrue(model.canBuild)
        window.orderOut(nil) // Registration must work even while the launcher is hidden.
        model.startBuild()
        wait(for: [completed], timeout: 10)
        withExtendedLifetime(token) {}
        XCTAssertTrue(model.done)
        XCTAssertFalse(model.running)
        XCTAssertEqual(model.stages.map(\.name), ["Mac app"])   // from the JSON events, which the log shows as text
        XCTAssertEqual(model.stages.first?.state, .done)
        XCTAssertEqual(model.stages.first?.detail, "Test build.app")
        XCTAssertTrue(model.logTail.contains("✓ Mac app (4s): Test build.app"))
        XCTAssertFalse(model.logTail.contains("\"ev\""))
        XCTAssertEqual(library.games.count, 1)
        XCTAssertTrue(library.games[0].isReady)
        XCTAssertEqual(library.games[0].inputs["base"], [base.path])
    }

    /// Opt-in: the README's screenshots (frontend/screenshots/launcher-*.png), framed as macOS windows. They show a copy of
    /// the user's library with its eShop key art (never the library itself) and no personal paths.
    /// NX_LAUNCHER_README_DIR=<dir> NX_LAUNCHER_README_SETTINGS=<a Mac launcher.json> swift test --filter testReadmeScreenshots
    @MainActor func testReadmeScreenshots() throws {
        guard let directory = ProcessInfo.processInfo.environment["NX_LAUNCHER_README_DIR"] else { throw XCTSkip("Set NX_LAUNCHER_README_DIR to render the README screenshots") }
        let output = URL(fileURLWithPath: directory)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        NSApplication.shared.setActivationPolicy(.accessory)
        let saved = ["repoPath", "buildTarget", "buildOutputDirectory"].map { ($0, UserDefaults.standard.object(forKey: $0)) }
        defer { for (key, value) in saved { UserDefaults.standard.set(value, forKey: key) } }
        let real = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("NS Port Builder/library.json")
        let copy = temporary.appendingPathComponent("library.json")
        // The games, without the user's folders: names and title IDs only, and BotW as a finished Mac build (a fixture app)
        let real_ = GameLibrary(file: real)
        let library = GameLibrary(file: copy)
        let botw = "01007EF00011E000", totk = "0100F2C0115B6000"
        let model = BuildModel()
        model.repoPath = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path
        model.lang = .en
        model.depsChecked = true
        model.deps = [BuildModel.Dep(id: "test", name: "Xcode", hint: "", present: true, installCmd: [])]
        for game in real_.games.sorted(by: { $0.name < $1.name }) {
            let name = game.name.replacingOccurrences(of: #"\s*\([0-9.]+ GB\)"#, with: "", options: .regularExpression)
            if game.titleID == botw { _ = try library.addApp(makeApp(botw, name: name)) }
            else { model.slots[0].paths = ["/games/\(name) [\(game.titleID)].nsp"]; _ = library.rememberInputs(model) }
        }
        model.slots[0].paths = []
        XCTAssertFalse(library.games.isEmpty, "the user's library is empty")
        let botwID = try XCTUnwrap(library.games.first { $0.titleID == botw }?.id)
        let totkID = library.games.first { $0.titleID == totk }?.id
        if let json = ProcessInfo.processInfo.environment["NX_LAUNCHER_README_SETTINGS"], let app = library.games.first(where: { $0.id == botwID })?.appPath {
            let dm = URL(fileURLWithPath: app).appendingPathComponent("Contents/Resources/dm")
            try FileManager.default.createDirectory(at: dm, withIntermediateDirectories: true)
            try FileManager.default.copyItem(at: URL(fileURLWithPath: json), to: dm.appendingPathComponent("launcher.json"))
        }

        /// The view at 2× in a window-shaped frame: rounded corners, the traffic lights, a soft shadow, transparent around.
        func shot<V: View>(_ name: String, _ view: V, size: NSSize, trafficLights: Bool = true) throws {
            let host = NSHostingView(rootView: view)
            let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.titled], backing: .buffered, defer: false)
            window.contentView = host
            window.orderFront(nil)
            RunLoop.main.run(until: Date().addingTimeInterval(1.2))   // artwork, layout
            host.layoutSubtreeIfNeeded()
            let scale: CGFloat = 2
            let content = try XCTUnwrap(NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * scale), pixelsHigh: Int(size.height * scale),
                                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0))
            content.size = size
            host.cacheDisplay(in: host.bounds, to: content)
            window.orderOut(nil)
            let pad: CGFloat = 56, radius: CGFloat = 14, full = NSSize(width: size.width + pad * 2, height: size.height + pad * 2)
            let framed = NSImage(size: full, flipped: false) { _ in
                let rect = NSRect(x: pad, y: pad, width: size.width, height: size.height)
                let shape = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
                NSGraphicsContext.saveGraphicsState()
                let shadow = NSShadow(); shadow.shadowColor = NSColor.black.withAlphaComponent(0.45); shadow.shadowBlurRadius = 36; shadow.shadowOffset = NSSize(width: 0, height: -14)
                shadow.set(); NSColor.black.setFill(); shape.fill()
                NSGraphicsContext.restoreGraphicsState()
                NSGraphicsContext.saveGraphicsState(); shape.addClip()
                content.draw(in: rect)
                for (i, color) in [NSColor(red: 1, green: 0.37, blue: 0.34, alpha: 1), NSColor(red: 1, green: 0.74, blue: 0.18, alpha: 1), NSColor(red: 0.16, green: 0.78, blue: 0.25, alpha: 1)].enumerated() where trafficLights {
                    color.setFill(); NSBezierPath(ovalIn: NSRect(x: rect.minX + 20 + CGFloat(i) * 20, y: rect.maxY - 20 - 12, width: 12, height: 12)).fill()
                }
                NSGraphicsContext.restoreGraphicsState()
                NSColor.white.withAlphaComponent(0.12).setStroke(); shape.lineWidth = 1; shape.stroke()
                return true
            }
            let out = try XCTUnwrap(NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(full.width * scale), pixelsHigh: Int(full.height * scale),
                                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0))
            out.size = full
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: out)
            framed.draw(in: NSRect(origin: .zero, size: full))
            NSGraphicsContext.restoreGraphicsState()
            try XCTUnwrap(out.representation(using: .png, properties: [:])).write(to: output.appendingPathComponent("launcher-\(name).png"))
        }
        let window = NSSize(width: 1280, height: 800)

        // The library while a Mac build of Tears of the Kingdom runs: the stage as the script reports it
        model.target = "mac-app"
        model.running = true
        model.stages = [Stage(id: 1, name: "build nx_extract", state: .done),
                        Stage(id: 2, name: "extract v1114112", state: .done, detail: "41,062 files, 18.3 GB"),
                        Stage(id: 3, name: "rewrite", state: .done, detail: "main, subsdk0, sdk"),
                        Stage(id: 4, name: "shader split", state: .running, detail: "splitting · 10,577 / 41,062 files · 41s left", percent: 26)]
        library.selectedID = botwID
        try shot("library", ContentView(model: model, library: library, activeBuildID: totkID, performStartupChecks: false), size: window)
        model.running = false; model.stages = []

        // The build page, ready to start: example paths, not the user's
        let dir = temporary.appendingPathComponent("games/Tears of the Kingdom")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let nsp = dir.appendingPathComponent("The Legend of Zelda Tears of the Kingdom [0100F2C0115B6000][v0].nsp")
        let update = dir.appendingPathComponent("The Legend of Zelda Tears of the Kingdom [0100F2C0115B6800][v1114112].nsp")
        let keys = dir.appendingPathComponent("prod.keys")
        for f in [nsp, update, keys] { try Data([0]).write(to: f) }
        model.slots[0].paths = [nsp.path]; model.slots[1].paths = [update.path]; model.slots[3].paths = [keys.path]
        model.gameDir = "/Volumes/Games/Tears of the Kingdom"
        model.outDir = "/Volumes/Games"
        try shot("build", ContentView(model: model, library: library, initialPage: .builds, performStartupChecks: false), size: NSSize(width: 1280, height: 940))

        if let upscale = LaunchSetting.read(app: library.games.first { $0.id == botwID }!.appPath)?.first(where: { $0.env == "DM_PRESENT_UPSCALE" }) {
            library.setLaunchOption(upscale, "metalfx", for: botwID)
        }
        try shot("launch-options", LaunchOptionsView(m: model, library: library, gameID: botwID), size: NSSize(width: 600, height: 700), trafficLights: false)   // a sheet: no window controls of its own
    }

    /// Opt-in native rendering for visual QA; it never touches the user's saved library.
    @MainActor func testRenderLauncherForVisualQA() throws {
        guard let directory = ProcessInfo.processInfo.environment["NX_LAUNCHER_CAPTURE_DIR"] else { throw XCTSkip("Set NX_LAUNCHER_CAPTURE_DIR to capture native views") }
        let output = URL(fileURLWithPath: directory)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let repo = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path
        let model = BuildModel()
        // Restore any preferences changed while preparing isolated preview data.
        let oldRepo = UserDefaults.standard.object(forKey: "repoPath")
        let oldOutput = UserDefaults.standard.object(forKey: "buildOutputDirectory")
        defer {
            UserDefaults.standard.set(oldRepo, forKey: "repoPath")
            UserDefaults.standard.set(oldOutput, forKey: "buildOutputDirectory")
        }
        model.repoPath = repo
        model.lang = .en
        model.outDir = temporary.path
        model.depsChecked = true
        model.deps = [BuildModel.Dep(id: "test", name: "Xcode", hint: "", present: true, installCmd: [])]
        let library = GameLibrary(file: temporary.appendingPathComponent("preview.json"))
        let examples = [(titleID, "Hades"), ("01007EF00011E000", "The Legend of Zelda: Breath of the Wild"),
                        ("0100000000010000", "Super Mario Odyssey"), ("010093801237C000", "Metroid Dread")]
        for (id, name) in examples.prefix(3) { _ = try library.addApp(makeApp(id, name: name)) }
        model.slots[0].paths = ["/preview/Metroid Dread [010093801237C000].nsp"]
        _ = library.rememberInputs(model)
        model.slots[0].paths = []
        library.selectedID = library.games.first?.id
        func capture(_ name: String, width: CGFloat = 1440, height: CGFloat = 1024, page: LauncherPage = .library, activeID: UUID? = nil, empty: Bool = false,
                     search: String? = nil) throws {
            let store = empty ? GameLibrary(file: temporary.appendingPathComponent("empty.json")) : library
            let view = ContentView(model: model, library: store, initialPage: page, activeBuildID: activeID, performStartupChecks: false, search: search)
            let host = NSHostingView(rootView: view)
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                                  styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            window.contentView = host
            window.orderFront(nil)
            RunLoop.main.run(until: Date().addingTimeInterval(0.5))
            host.layoutSubtreeIfNeeded()
            let bounds = host.bounds
            let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: bounds))
            host.cacheDisplay(in: bounds, to: rep)
            try XCTUnwrap(rep.representation(using: .png, properties: [:])).write(to: output.appendingPathComponent(name + ".png"))
            window.orderOut(nil)
        }
        try capture("library")
        try capture("library-search", search: "zel")
        try capture("library-small", width: 900, height: 680)
        try capture("empty", empty: true)
        try capture("build", page: .builds)
        try capture("build-small", width: 900, height: 680, page: .builds)
        try capture("settings", page: .settings)
        if let json = ProcessInfo.processInfo.environment["NX_LAUNCHER_CAPTURE_SETTINGS"] {   // a launcher.json from dm/games/settings.py
            let first = library.games[0]
            let dm = URL(fileURLWithPath: first.appPath).appendingPathComponent("Contents/Resources/dm")
            try FileManager.default.createDirectory(at: dm, withIntermediateDirectories: true)
            try FileManager.default.copyItem(at: URL(fileURLWithPath: json), to: dm.appendingPathComponent("launcher.json"))
            if let fps = LaunchSetting.read(app: first.appPath)?.first(where: { $0.env == "DM_FPS_TARGET" }) { library.setLaunchOption(fps, "60", for: first.id) }
            let host = NSHostingView(rootView: LaunchOptionsView(m: model, library: library, gameID: first.id).environmentObject(PadFocus()))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 700), styleMask: [.titled], backing: .buffered, defer: false)
            window.contentView = host
            window.orderFront(nil)
            RunLoop.main.run(until: Date().addingTimeInterval(0.5))
            let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
            host.cacheDisplay(in: host.bounds, to: rep)
            try XCTUnwrap(rep.representation(using: .png, properties: [:])).write(to: output.appendingPathComponent("launch-options.png"))
            window.orderOut(nil)
            // The same sheet with a controller's focus on the console mode row
            let focus = PadFocus()
            let focused = NSHostingView(rootView: LaunchOptionsView(m: model, library: library, gameID: first.id).environmentObject(focus))
            window.contentView = focused
            window.orderFront(nil)
            RunLoop.main.run(until: Date().addingTimeInterval(0.4))
            focus.scope = "launch"
            focus.show(default: "opt.DM_OPMODE")
            RunLoop.main.run(until: Date().addingTimeInterval(0.4))
            let focusRep = try XCTUnwrap(focused.bitmapImageRepForCachingDisplay(in: focused.bounds))
            focused.cacheDisplay(in: focused.bounds, to: focusRep)
            try XCTUnwrap(focusRep.representation(using: .png, properties: [:])).write(to: output.appendingPathComponent("launch-options-focus.png"))
            window.orderOut(nil)
        }
        do {   // the Controls sheet with a few changes, kept in a suite of its own
            let suite = "nxnative-visual-qa-controls"
            let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
            defaults.removePersistentDomain(forName: suite)
            defer { defaults.removePersistentDomain(forName: suite) }
            let layout = ControlLayout(defaults: defaults)
            if let up = ControlLayout.functions.first(where: { $0.id == "RUp" }) { layout.assign(up, key: 34) }
            layout.setSends("A", "B"); layout.setSends("B", "A")
            let host = NSHostingView(rootView: ControlsView(m: model, layout: layout).environmentObject(PadFocus()))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 700), styleMask: [.titled], backing: .buffered, defer: false)
            window.contentView = host
            window.orderFront(nil)
            RunLoop.main.run(until: Date().addingTimeInterval(0.5))
            let rep = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
            host.cacheDisplay(in: host.bounds, to: rep)
            try XCTUnwrap(rep.representation(using: .png, properties: [:])).write(to: output.appendingPathComponent("controls.png"))
            window.orderOut(nil)
        }
        let crowded = GameLibrary(file: temporary.appendingPathComponent("crowded.json"))   // more games than the row shows: its pager chevrons
        for (index, name) in ["Hades", "Metroid Dread", "Bayonetta 3", "Pikmin 4", "Splatoon 3", "Kirby and the Forgotten Land", "Xenoblade Chronicles 3", "Fire Emblem Engage"].enumerated() {
            model.slots[0].paths = ["/preview/\(name) [01000000000\(String(format: "%02X", index))000].nsp"]
            _ = crowded.rememberInputs(model)
        }
        model.slots[0].paths = []
        let view = ContentView(model: model, library: crowded, performStartupChecks: false)
        let crowdedHost = NSHostingView(rootView: view)
        let crowdedWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 900), styleMask: [.titled], backing: .buffered, defer: false)
        crowdedWindow.contentView = crowdedHost
        crowdedWindow.orderFront(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))
        let crowdedRep = try XCTUnwrap(crowdedHost.bitmapImageRepForCachingDisplay(in: crowdedHost.bounds))
        crowdedHost.cacheDisplay(in: crowdedHost.bounds, to: crowdedRep)
        try XCTUnwrap(crowdedRep.representation(using: .png, properties: [:])).write(to: output.appendingPathComponent("library-crowded.png"))
        func scrollViews(_ view: NSView) -> [NSScrollView] { ((view as? NSScrollView).map { [$0] } ?? []) + view.subviews.flatMap(scrollViews) }
        if let row = scrollViews(crowdedHost).first(where: { $0.documentView.map { $0.frame.width > $0.frame.height * 3 } ?? false }) {
            row.contentView.scroll(to: NSPoint(x: 600, y: row.contentView.bounds.origin.y))   // scrolled: the left chevron too
            row.reflectScrolledClipView(row.contentView)
            RunLoop.main.run(until: Date().addingTimeInterval(0.5))
            let scrolledRep = try XCTUnwrap(crowdedHost.bitmapImageRepForCachingDisplay(in: crowdedHost.bounds))
            crowdedHost.cacheDisplay(in: crowdedHost.bounds, to: scrolledRep)
            try XCTUnwrap(scrolledRep.representation(using: .png, properties: [:])).write(to: output.appendingPathComponent("library-crowded-scrolled.png"))
        }
        crowdedWindow.orderOut(nil)
        let bars = NSHostingView(rootView: VStack(spacing: 18) {   // a step with a percentage, and one without
            LauncherProgressBar(value: 0.4).frame(width: 480)
            LauncherProgressBar(value: nil).frame(width: 480)
            LauncherProgressBar(value: nil, height: 6).frame(width: 180)
        }.padding(30).background(LauncherPalette.background))
        bars.frame = NSRect(x: 0, y: 0, width: 540, height: 130)
        let barsWindow = NSWindow(contentRect: bars.frame, styleMask: [.titled], backing: .buffered, defer: false)
        barsWindow.contentView = bars
        barsWindow.orderFront(nil)
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
        let barsRep = try XCTUnwrap(bars.bitmapImageRepForCachingDisplay(in: bars.bounds))
        bars.cacheDisplay(in: bars.bounds, to: barsRep)
        try XCTUnwrap(barsRep.representation(using: .png, properties: [:])).write(to: output.appendingPathComponent("progress-bars.png"))
        barsWindow.orderOut(nil)
        model.running = true
        model.stages = [Stage(id: 1, name: "Extract game files", state: .running, percent: 40)]
        try capture("building", activeID: library.games[1].id)
        model.running = false
    }
}
