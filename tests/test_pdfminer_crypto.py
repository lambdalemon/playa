"""Test of various compression/encoding modules (previously in doctests)"""

import binascii

from playa.arcfour import Arcfour
from playa.ascii85 import ascii85decode, asciihexdecode
from playa.lzw import lzwdecode
from playa.runlength import rldecode


def hex(b):
    """encode('hex')"""
    return binascii.hexlify(b)


def dehex(b):
    """decode('hex')"""
    return binascii.unhexlify(b)


class TestAscii85:
    def test_ascii85decode(self):
        """The sample string is taken from:
        http://en.wikipedia.org/w/index.php?title=Ascii85
        """
        assert ascii85decode(b"9jqo^BlbD-BleB1DJ+*+F(f,q") == b"Man is distinguished"
        assert ascii85decode(b"E,9)oF*2M7/c~>") == b"pleasure."
        assert ascii85decode(b"zE,9)oF*2M7/c~>") == b"\0\0\0\0pleasure."
        # And some bogus cases you may encounter
        assert ascii85decode(b"E,9)oF*2M7/c~") == b"pleasure."
        assert ascii85decode(b"<~E,9)oF*2M7/c~") == b"pleasure."
        assert ascii85decode(b"<~E,9)oF*2M7/c~\n>") == b"pleasure."
        # Ensure that we don't miss actual ASCII85 digits
        assert (
            ascii85decode(b"<^BVT:K:=9<E)pd;BS_1:/aSV;ag~>")
            == b"VARIOUS UTTER NONSENSE"
        )
        assert (
            ascii85decode(b"<~<^BVT:K:=9<E)pd;BS_1:/aSV;ag~>")
            == b"VARIOUS UTTER NONSENSE"
        )
        assert (
            ascii85decode(b"<^BVT:K:=9<E)pd;BS_1:/aSV;ag~") == b"VARIOUS UTTER NONSENSE"
        )

    def test_asciihexdecode(self):
        assert asciihexdecode(b"61 62 2e6364   65") == b"ab.cde"
        assert asciihexdecode(b"61 62 2e6364   657>") == b"ab.cdep"
        assert asciihexdecode(b"7>") == b"p"


class TestArcfour:
    def test(self):
        assert hex(Arcfour(b"Key").process(b"Plaintext")) == b"bbf316e8d940af0ad3"
        assert hex(Arcfour(b"Wiki").process(b"pedia")) == b"1021bf0420"
        assert (
            hex(Arcfour(b"Secret").process(b"Attack at dawn"))
            == b"45a01f645fc35b383552544b9bf5"
        )


class TestLzw:
    def test_lzwdecode(self):
        assert (
            lzwdecode(b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01")
            == b"\x2d\x2d\x2d\x2d\x2d\x41\x2d\x2d\x2d\x42"
        )


class TestRunlength:
    def test_rldecode(self):
        assert rldecode(b"\x05123456\xfa7\x04abcde\x80junk") == b"1234567777777abcde"
