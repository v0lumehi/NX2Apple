"""The text scan must start at the text segment's offset, not at 0 (GitHub issue #14).

An NRO file is the image itself preceded by a header: its text segment starts at 0x80, while an
NSO's text starts at 0 after decompression. Scanning an NRO from 0 read the header's build ID as
instructions and reported `44 ldr x18, #0xfffffffffff40370` on Final Fantasy X/X-2 HD Remaster.

Testing the real code path end to end needs a structurally complete NRO (MOD0 block, FDE tables),
which is a lot of fixture for the question "does the lower bound hold". These tests check the two
things that were wrong instead:

1. `nso_code.data_blocks(..., lo=N)` never returns a block that starts below N.
2. `patch_nso.py` passes the text segment's own offset as that bound, which is 0x80 for an NRO and
   0 for an NSO — asserted on the source, since the value comes from the file being rewritten.
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

import nso_code   # noqa: E402

TOOLS = HERE.parents[1]


class DataBlockLowerBound(unittest.TestCase):
    def test_gaps_start_at_lo(self):
        """The first gap must begin at `lo`, not at 0.

        data_blocks() derives its gaps from the FDE table, which needs a structurally complete
        NRO to exercise. The bound itself is one line — `gaps, last = [], lo` — so it is checked
        by patching fde_ranges to report one covered range and reading the gaps back.
        """
        real = nso_code.fde_ranges
        try:
            nso_code.fde_ranges = lambda img: [(0x100, 0x200)]
            from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
            md = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
            img = bytes(0x400)
            # text_end 0x300, lo 0x80: the gaps are [0x80,0x100) and [0x200,0x300), never below 0x80
            blocks = nso_code.data_blocks(img, 0x300, md, lo=0x80)
            self.assertTrue(all(s >= 0x80 for s, _ in blocks),
                            f'a block starts below the text segment: {blocks}')
            blocks0 = nso_code.data_blocks(img, 0x300, md)
            self.assertTrue(all(s >= 0 for s, _ in blocks0), 'default lo must not break NSOs')
        finally:
            nso_code.fde_ranges = real

    def test_lo_defaults_to_zero(self):
        """An NSO keeps the old behaviour: no header, so the scan starts at 0."""
        self.assertEqual(nso_code.data_blocks.__defaults__, (0,),
                         'data_blocks must default lo to 0 so the NSO path is unchanged')


# PatchNsoPassesTheOffset asserted that patch_nso.py passes TEXT_LO = segs[0][1] to data_blocks. The sync fe56176 took upstream's
# patch_nso.py (not a carrier), so that call is gone and the scan starts at 0 again. It is not put back here: an NRO's text segment
# starts at memory offset 0 with the 0x80-byte header inside it, so segs[0][1] is 0 and that bound never skipped the header. The
# fix (a bound of 0x80 for an NRO) belongs in upstream's patch_nso.py; until then issue #14 is open again.


if __name__ == '__main__':
    unittest.main(verbosity=2)
