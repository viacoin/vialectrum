# -*- coding: utf-8 -*-
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import hashlib
import base64
import hmac
import os
import json

import ecdsa
import pyaes

from .util import bfh, bh2u, to_string
from . import version
from .util import print_error, InvalidPassword, assert_bytes, to_bytes, inv_dict
from . import segwit_addr
from . import constants


################################## transactions

COINBASE_MATURITY = 100
COIN = 100000000

# supported types of transction outputs
TYPE_ADDRESS = 0
TYPE_PUBKEY  = 1
TYPE_SCRIPT  = 2

# AES encryption
try:
    from Cryptodome.Cipher import AES
except:
    AES = None


class InvalidPadding(Exception):
    pass


def append_PKCS7_padding(data):
    assert_bytes(data)
    padlen = 16 - (len(data) % 16)
    return data + bytes([padlen]) * padlen


def strip_PKCS7_padding(data):
    assert_bytes(data)
    if len(data) % 16 != 0 or len(data) == 0:
        raise InvalidPadding("invalid length")
    padlen = data[-1]
    if padlen > 16:
        raise InvalidPadding("invalid padding byte (large)")
    for i in data[-padlen:]:
        if i != padlen:
            raise InvalidPadding("invalid padding byte (inconsistent)")
    return data[0:-padlen]


def aes_encrypt_with_iv(key, iv, data):
    assert_bytes(key, iv, data)
    data = append_PKCS7_padding(data)
    if AES:
        e = AES.new(key, AES.MODE_CBC, iv).encrypt(data)
    else:
        aes_cbc = pyaes.AESModeOfOperationCBC(key, iv=iv)
        aes = pyaes.Encrypter(aes_cbc, padding=pyaes.PADDING_NONE)
        e = aes.feed(data) + aes.feed()  # empty aes.feed() flushes buffer
    return e


def aes_decrypt_with_iv(key, iv, data):
    assert_bytes(key, iv, data)
    if AES:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        data = cipher.decrypt(data)
    else:
        aes_cbc = pyaes.AESModeOfOperationCBC(key, iv=iv)
        aes = pyaes.Decrypter(aes_cbc, padding=pyaes.PADDING_NONE)
        data = aes.feed(data) + aes.feed()  # empty aes.feed() flushes buffer
    try:
        return strip_PKCS7_padding(data)
    except InvalidPadding:
        raise InvalidPassword()


def EncodeAES(secret, s):
    assert_bytes(s)
    iv = bytes(os.urandom(16))
    ct = aes_encrypt_with_iv(secret, iv, s)
    e = iv + ct
    return base64.b64encode(e)

def DecodeAES(secret, e):
    e = bytes(base64.b64decode(e))
    iv, e = e[:16], e[16:]
    s = aes_decrypt_with_iv(secret, iv, e)
    return s

def pw_encode(s, password):
    if password:
        secret = Hash(password)
        return EncodeAES(secret, to_bytes(s, "utf8")).decode('utf8')
    else:
        return s

def pw_decode(s, password):
    if password is not None:
        secret = Hash(password)
        try:
            d = to_string(DecodeAES(secret, s), "utf8")
        except Exception:
            raise InvalidPassword()
        return d
    else:
        return s


def rev_hex(s):
    return bh2u(bfh(s)[::-1])


def int_to_hex(i, length=1):
    assert isinstance(i, int)
    s = hex(i)[2:].rstrip('L')
    s = "0"*(2*length - len(s)) + s
    return rev_hex(s)


def var_int(i):
    # https://en.bitcoin.it/wiki/Protocol_specification#Variable_length_integer
    if i<0xfd:
        return int_to_hex(i)
    elif i<=0xffff:
        return "fd"+int_to_hex(i,2)
    elif i<=0xffffffff:
        return "fe"+int_to_hex(i,4)
    else:
        return "ff"+int_to_hex(i,8)


def op_push(i):
    if i<0x4c:
        return int_to_hex(i)
    elif i<0xff:
        return '4c' + int_to_hex(i)
    elif i<0xffff:
        return '4d' + int_to_hex(i,2)
    else:
        return '4e' + int_to_hex(i,4)

def push_script(x):
    return op_push(len(x)//2) + x

def sha256(x):
    x = to_bytes(x, 'utf8')
    return bytes(hashlib.sha256(x).digest())


def Hash(x):
    x = to_bytes(x, 'utf8')
    out = bytes(sha256(sha256(x)))
    return out


hash_encode = lambda x: bh2u(x[::-1])
hash_decode = lambda x: bfh(x)[::-1]
hmac_sha_512 = lambda x, y: hmac.new(x, y, hashlib.sha512).digest()


def is_new_seed(x, prefix=version.SEED_PREFIX):
    from . import mnemonic
    x = mnemonic.normalize_text(x)
    s = bh2u(hmac_sha_512(b"Seed version", x.encode('utf8')))
    return s.startswith(prefix)


def is_old_seed(seed):
    from . import old_mnemonic, mnemonic
    seed = mnemonic.normalize_text(seed)
    words = seed.split()
    try:
        # checks here are deliberately left weak for legacy reasons, see #3149
        old_mnemonic.mn_decode(words)
        uses_electrum_words = True
    except Exception:
        uses_electrum_words = False
    try:
        seed = bfh(seed)
        is_hex = (len(seed) == 16 or len(seed) == 32)
    except Exception:
        is_hex = False
    return is_hex or (uses_electrum_words and (len(words) == 12 or len(words) == 24))


def seed_type(x):
    if is_old_seed(x):
        return 'old'
    elif is_new_seed(x):
        return 'standard'
    elif is_new_seed(x, version.SEED_PREFIX_SW):
        return 'segwit'
    elif is_new_seed(x, version.SEED_PREFIX_2FA):
        return '2fa'
    return ''

is_seed = lambda x: bool(seed_type(x))

# pywallet openssl private key implementation

def i2o_ECPublicKey(pubkey, compressed=False):
    # public keys are 65 bytes long (520 bits)
    # 0x04 + 32-byte X-coordinate + 32-byte Y-coordinate
    # 0x00 = point at infinity, 0x02 and 0x03 = compressed, 0x04 = uncompressed
    # compressed keys: <sign> <x> where <sign> is 0x02 if y is even and 0x03 if y is odd
    if compressed:
        if pubkey.point.y() & 1:
            key = '03' + '%064x' % pubkey.point.x()
        else:
            key = '02' + '%064x' % pubkey.point.x()
    else:
        key = '04' + \
              '%064x' % pubkey.point.x() + \
              '%064x' % pubkey.point.y()

    return bfh(key)
# end pywallet openssl private key implementation


############ functions from pywallet #####################
def hash_160(public_key):
    try:
        md = hashlib.new('ripemd160')
        md.update(sha256(public_key))
        return md.digest()
    except BaseException:
        from . import ripemd
        md = ripemd.new(sha256(public_key))
        return md.digest()


def hash160_to_b58_address(h160, addrtype, witness_program_version=1):
    s = bytes([addrtype])
    s += h160
    return base_encode(s+Hash(s)[0:4], base=58)


def b58_address_to_hash160(addr):
    addr = to_bytes(addr, 'ascii')
    _bytes = base_decode(addr, 25, base=58)
    return _bytes[0], _bytes[1:21]


def hash160_to_p2pkh(h160):
    return hash160_to_b58_address(h160, constants.net.ADDRTYPE_P2PKH)

def hash160_to_p2sh(h160):
    return hash160_to_b58_address(h160, constants.net.ADDRTYPE_P2SH)

def public_key_to_p2pkh(public_key):
    return hash160_to_p2pkh(hash_160(public_key))

def hash_to_segwit_addr(h):
    return segwit_addr.encode(constants.net.SEGWIT_HRP, 0, h)

def public_key_to_p2wpkh(public_key):
    return hash_to_segwit_addr(hash_160(public_key))

def script_to_p2wsh(script):
    return hash_to_segwit_addr(sha256(bfh(script)))

def p2wpkh_nested_script(pubkey):
    pkh = bh2u(hash_160(bfh(pubkey)))
    return '00' + push_script(pkh)

def p2wsh_nested_script(witness_script):
    wsh = bh2u(sha256(bfh(witness_script)))
    return '00' + push_script(wsh)

def pubkey_to_address(txin_type, pubkey):
    if txin_type == 'p2pkh':
        return public_key_to_p2pkh(bfh(pubkey))
    elif txin_type == 'p2wpkh':
        return hash_to_segwit_addr(hash_160(bfh(pubkey)))
    elif txin_type == 'p2wpkh-p2sh':
        scriptSig = p2wpkh_nested_script(pubkey)
        return hash160_to_p2sh(hash_160(bfh(scriptSig)))
    else:
        raise NotImplementedError(txin_type)

def redeem_script_to_address(txin_type, redeem_script):
    if txin_type == 'p2sh':
        return hash160_to_p2sh(hash_160(bfh(redeem_script)))
    elif txin_type == 'p2wsh':
        return script_to_p2wsh(redeem_script)
    elif txin_type == 'p2wsh-p2sh':
        scriptSig = p2wsh_nested_script(redeem_script)
        return hash160_to_p2sh(hash_160(bfh(scriptSig)))
    else:
        raise NotImplementedError(txin_type)


def script_to_address(script):
    from .transaction import get_address_from_output_script
    t, addr = get_address_from_output_script(bfh(script))
    assert t == TYPE_ADDRESS
    return addr

def address_to_script(addr):
    witver, witprog = segwit_addr.decode(constants.net.SEGWIT_HRP, addr)
    if witprog is not None:
        assert (0 <= witver <= 16)
        OP_n = witver + 0x50 if witver > 0 else 0
        script = bh2u(bytes([OP_n]))
        script += push_script(bh2u(bytes(witprog)))
        return script
    addrtype, hash_160 = b58_address_to_hash160(addr)
    if addrtype == constants.net.ADDRTYPE_P2PKH:
        script = '76a9'                                      # op_dup, op_hash_160
        script += push_script(bh2u(hash_160))
        script += '88ac'                                     # op_equalverify, op_checksig
    elif addrtype == constants.net.ADDRTYPE_P2SH:
        script = 'a9'                                        # op_hash_160
        script += push_script(bh2u(hash_160))
        script += '87'                                       # op_equal
    else:
        raise BaseException('unknown address type')
    return script

def address_to_scripthash(addr):
    script = address_to_script(addr)
    return script_to_scripthash(script)

def script_to_scripthash(script):
    h = sha256(bytes.fromhex(script))[0:32]
    return bh2u(bytes(reversed(h)))

def public_key_to_p2pk_script(pubkey):
    script = push_script(pubkey)
    script += 'ac'                                           # op_checksig
    return script

__b58chars = b'123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
assert len(__b58chars) == 58

__b43chars = b'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ$*+-./:'
assert len(__b43chars) == 43


def base_encode(v, base):
    """ encode v, which is a string of bytes, to base58."""
    assert_bytes(v)
    assert base in (58, 43)
    chars = __b58chars
    if base == 43:
        chars = __b43chars
    long_value = 0
    for (i, c) in enumerate(v[::-1]):
        long_value += (256**i) * c
    result = bytearray()
    while long_value >= base:
        div, mod = divmod(long_value, base)
        result.append(chars[mod])
        long_value = div
    result.append(chars[long_value])
    # Bitcoin does a little leading-zero-compression:
    # leading 0-bytes in the input become leading-1s
    nPad = 0
    for c in v:
        if c == 0x00:
            nPad += 1
        else:
            break
    result.extend([chars[0]] * nPad)
    result.reverse()
    return result.decode('ascii')


def base_decode(v, length, base):
    """ decode v into a string of len bytes."""
    # assert_bytes(v)
    v = to_bytes(v, 'ascii')
    assert base in (58, 43)
    chars = __b58chars
    if base == 43:
        chars = __b43chars
    long_value = 0
    for (i, c) in enumerate(v[::-1]):
        long_value += chars.find(bytes([c])) * (base**i)
    result = bytearray()
    while long_value >= 256:
        div, mod = divmod(long_value, 256)
        result.append(mod)
        long_value = div
    result.append(long_value)
    nPad = 0
    for c in v:
        if c == chars[0]:
            nPad += 1
        else:
            break
    result.extend(b'\x00' * nPad)
    if length is not None and len(result) != length:
        return None
    result.reverse()
    return bytes(result)


def EncodeBase58Check(vchIn):
    hash = Hash(vchIn)
    return base_encode(vchIn + hash[0:4], base=58)


def DecodeBase58Check(psz):
    vchRet = base_decode(psz, None, base=58)
    key = vchRet[0:-4]
    csum = vchRet[-4:]
    hash = Hash(key)
    cs32 = hash[0:4]
    if cs32 != csum:
        return None
    else:
        return key


# backwards compat
# extended WIF for segwit (used in 3.0.x; but still used internally)
SCRIPT_TYPES = {
    'p2pkh':48,
    'p2wpkh':1,
    'p2wpkh-p2sh':2,
    'p2sh':50,
    'p2wsh':6,
    'p2wsh-p2sh':7
}


def serialize_privkey(secret, compressed, txin_type, internal_use=False):
    if internal_use:
        prefix = bytes([(SCRIPT_TYPES[txin_type] + constants.net.WIF_PREFIX) & 255])
    else:
        prefix = bytes([(SCRIPT_TYPES['p2pkh'] + constants.net.WIF_PREFIX) & 255])
    suffix = b'\01' if compressed else b''
    vchIn = prefix + secret + suffix
    base58_wif = EncodeBase58Check(vchIn)
    if internal_use:
        return base58_wif
    else:
        return '{}:{}'.format(txin_type, base58_wif)


def deserialize_privkey(key):
    if is_minikey(key):
        return 'p2pkh', minikey_to_private_key(key), True

    txin_type = None
    if ':' in key:
        txin_type, key = key.split(sep=':', maxsplit=1)
        assert txin_type in SCRIPT_TYPES
    vch = DecodeBase58Check(key)
    if not vch:
        neutered_privkey = str(key)[:3] + '..' + str(key)[-2:]
        raise BaseException("cannot deserialize", neutered_privkey)

    if txin_type is None:
        # keys exported in version 3.0.x encoded script type in first byte
        txin_type = inv_dict(SCRIPT_TYPES)[vch[0] - constants.net.WIF_PREFIX]
    else:
        assert vch[0] == (SCRIPT_TYPES['p2pkh'] + constants.net.WIF_PREFIX) & 255

    assert len(vch) in [33, 34]
    compressed = len(vch) == 34
    return txin_type, vch[1:33], compressed


def regenerate_key(pk):
    assert len(pk) == 32
    return EC_KEY(pk)


def GetPubKey(pubkey, compressed=False):
    return i2o_ECPublicKey(pubkey, compressed)


def GetSecret(pkey):
    return bfh('%064x' % pkey.secret)


def is_compressed(sec):
    return deserialize_privkey(sec)[2]


def public_key_from_private_key(pk, compressed):
    pkey = regenerate_key(pk)
    public_key = GetPubKey(pkey.pubkey, compressed)
    return bh2u(public_key)

def address_from_private_key(sec):
    txin_type, privkey, compressed = deserialize_privkey(sec)
    public_key = public_key_from_private_key(privkey, compressed)
    return pubkey_to_address(txin_type, public_key)

def is_segwit_address(addr):
    try:
        witver, witprog = segwit_addr.decode(constants.net.SEGWIT_HRP, addr)
    except Exception as e:
        return False
    return witprog is not None

def is_b58_address(addr):
    try:
        addrtype, h = b58_address_to_hash160(addr)
    except Exception as e:
        return False
    if addrtype not in [constants.net.ADDRTYPE_P2PKH, constants.net.ADDRTYPE_P2SH]:
        return False
    return addr == hash160_to_b58_address(h, addrtype)

def is_address(addr):
    return is_segwit_address(addr) or is_b58_address(addr)


def is_private_key(key):
    try:
        k = deserialize_privkey(key)
        return k is not False
    except:
        return False


########### end pywallet functions #######################

def is_minikey(text):
    # Minikeys are typically 22 or 30 characters, but this routine
    # permits any length of 20 or more provided the minikey is valid.
    # A valid minikey must begin with an 'S', be in base58, and when
    # suffixed with '?' have its SHA256 hash begin with a zero byte.
    # They are widely used in Casascius physical bitcoins.
    return (len(text) >= 20 and text[0] == 'S'
            and all(ord(c) in __b58chars for c in text)
            and sha256(text + '?')[0] == 0x00)

def minikey_to_private_key(text):
    return sha256(text)

from ecdsa.ecdsa import curve_secp256k1, generator_secp256k1
from ecdsa.curves import SECP256k1
from ecdsa.ellipticcurve import Point
from ecdsa.util import string_to_number, number_to_string


def msg_magic(message):
    length = bfh(var_int(len(message)))
    return b"\x19Litecoin Signed Message:\n" + length + message


def verify_message(address, sig, message):
    assert_bytes(sig, message)
    try:
        h = Hash(msg_magic(message))
        public_key, compressed = pubkey_from_signature(sig, h)
        # check public key using the address
        pubkey = point_to_ser(public_key.pubkey.point, compressed)
        for txin_type in ['p2pkh','p2wpkh','p2wpkh-p2sh']:
            addr = pubkey_to_address(txin_type, bh2u(pubkey))
            if address == addr:
                break
        else:
            raise Exception("Bad signature")
        # check message
        public_key.verify_digest(sig[1:], h, sigdecode = ecdsa.util.sigdecode_string)
        return True
    except Exception as e:
        print_error("Verification error: {0}".format(e))
        return False


def encrypt_message(message, pubkey, magic=b'BIE1'):
    return EC_KEY.encrypt_message(message, bfh(pubkey), magic)


def chunks(l, n):
    return [l[i:i+n] for i in range(0, len(l), n)]


def ECC_YfromX(x,curved=curve_secp256k1, odd=True):
    _p = curved.p()
    _a = curved.a()
    _b = curved.b()
    for offset in range(128):
        Mx = x + offset
        My2 = pow(Mx, 3, _p) + _a * pow(Mx, 2, _p) + _b % _p
        My = pow(My2, (_p+1)//4, _p )

        if curved.contains_point(Mx,My):
            if odd == bool(My&1):
                return [My,offset]
            return [_p-My,offset]
    raise Exception('ECC_YfromX: No Y found')


def negative_point(P):
    return Point( P.curve(), P.x(), -P.y(), P.order() )


def point_to_ser(P, comp=True ):
    if comp:
        return bfh( ('%02x'%(2+(P.y()&1)))+('%064x'%P.x()) )
    return bfh( '04'+('%064x'%P.x())+('%064x'%P.y()) )


def ser_to_point(Aser):
    curve = curve_secp256k1
    generator = generator_secp256k1
    _r  = generator.order()
    assert Aser[0] in [0x02, 0x03, 0x04]
    if Aser[0] == 0x04:
        return Point( curve, string_to_number(Aser[1:33]), string_to_number(Aser[33:]), _r )
    Mx = string_to_number(Aser[1:])
    return Point( curve, Mx, ECC_YfromX(Mx, curve, Aser[0] == 0x03)[0], _r )


class MyVerifyingKey(ecdsa.VerifyingKey):
    @classmethod
    def from_signature(klass, sig, recid, h, curve):
        """ See http://www.secg.org/download/aid-780/sec1-v2.pdf, chapter 4.1.6 """
        from ecdsa import util, numbertheory
        from . import msqr
        curveFp = curve.curve
        G = curve.generator
        order = G.order()
        # extract r,s from signature
        r, s = util.sigdecode_string(sig, order)
        # 1.1
        x = r + (recid//2) * order
        # 1.3
        alpha = ( x * x * x  + curveFp.a() * x + curveFp.b() ) % curveFp.p()
        beta = msqr.modular_sqrt(alpha, curveFp.p())
        y = beta if (beta - recid) % 2 == 0 else curveFp.p() - beta
        # 1.4 the constructor checks that nR is at infinity
        R = Point(curveFp, x, y, order)
        # 1.5 compute e from message:
        e = string_to_number(h)
        minus_e = -e % order
        # 1.6 compute Q = r^-1 (sR - eG)
        inv_r = numbertheory.inverse_mod(r,order)
        Q = inv_r * ( s * R + minus_e * G )
        return klass.from_public_point( Q, curve )


def pubkey_from_signature(sig, h):
    if len(sig) != 65:
        raise Exception("Wrong encoding")
    nV = sig[0]
    if nV < 27 or nV >= 35:
        raise Exception("Bad encoding")
    if nV >= 31:
        compressed = True
        nV -= 4
    else:
        compressed = False
    recid = nV - 27
    return MyVerifyingKey.from_signature(sig[1:], recid, h, curve = SECP256k1), compressed


class MySigningKey(ecdsa.SigningKey):
    """Enforce low S values in signatures"""

    def sign_number(self, number, entropy=None, k=None):
        curve = SECP256k1
        G = curve.generator
        order = G.order()
        r, s = ecdsa.SigningKey.sign_number(self, number, entropy, k)
        if s > order//2:
            s = order - s
        return r, s


class EC_KEY(object):

    def __init__( self, k ):
        secret = string_to_number(k)
        self.pubkey = ecdsa.ecdsa.Public_key( generator_secp256k1, generator_secp256k1 * secret )
        self.privkey = ecdsa.ecdsa.Private_key( self.pubkey, secret )
        self.secret = secret

    def get_public_key(self, compressed=True):
        return bh2u(point_to_ser(self.pubkey.point, compressed))

    def sign(self, msg_hash):
        private_key = MySigningKey.from_secret_exponent(self.secret, curve = SECP256k1)
        public_key = private_key.get_verifying_key()
        signature = private_key.sign_digest_deterministic(msg_hash, hashfunc=hashlib.sha256, sigencode = ecdsa.util.sigencode_string)
        assert public_key.verify_digest(signature, msg_hash, sigdecode = ecdsa.util.sigdecode_string)
        return signature

    def sign_message(self, message, is_compressed):
        message = to_bytes(message, 'utf8')
        signature = self.sign(Hash(msg_magic(message)))
        for i in range(4):
            sig = bytes([27 + i + (4 if is_compressed else 0)]) + signature
            try:
                self.verify_message(sig, message)
                return sig
            except Exception as e:
                continue
        else:
            raise Exception("error: cannot sign message")

    def verify_message(self, sig, message):
        assert_bytes(message)
        h = Hash(msg_magic(message))
        public_key, compressed = pubkey_from_signature(sig, h)
        # check public key
        if point_to_ser(public_key.pubkey.point, compressed) != point_to_ser(self.pubkey.point, compressed):
            raise Exception("Bad signature")
        # check message
        public_key.verify_digest(sig[1:], h, sigdecode = ecdsa.util.sigdecode_string)


    # ECIES encryption/decryption methods; AES-128-CBC with PKCS7 is used as the cipher; hmac-sha256 is used as the mac

    @classmethod
    def encrypt_message(self, message, pubkey, magic=b'BIE1'):
        assert_bytes(message)

        pk = ser_to_point(pubkey)
        if not ecdsa.ecdsa.point_is_valid(generator_secp256k1, pk.x(), pk.y()):
            raise Exception('invalid pubkey')

        ephemeral_exponent = number_to_string(ecdsa.util.randrange(pow(2,256)), generator_secp256k1.order())
        ephemeral = EC_KEY(ephemeral_exponent)
        ecdh_key = point_to_ser(pk * ephemeral.privkey.secret_multiplier)
        key = hashlib.sha512(ecdh_key).digest()
        iv, key_e, key_m = key[0:16], key[16:32], key[32:]
        ciphertext = aes_encrypt_with_iv(key_e, iv, message)
        ephemeral_pubkey = bfh(ephemeral.get_public_key(compressed=True))
        encrypted = magic + ephemeral_pubkey + ciphertext
        mac = hmac.new(key_m, encrypted, hashlib.sha256).digest()

        return base64.b64encode(encrypted + mac)

    def decrypt_message(self, encrypted, magic=b'BIE1'):
        encrypted = base64.b64decode(encrypted)
        if len(encrypted) < 85:
            raise Exception('invalid ciphertext: length')
        magic_found = encrypted[:4]
        ephemeral_pubkey = encrypted[4:37]
        ciphertext = encrypted[37:-32]
        mac = encrypted[-32:]
        if magic_found != magic:
            raise Exception('invalid ciphertext: invalid magic bytes')
        try:
            ephemeral_pubkey = ser_to_point(ephemeral_pubkey)
        except AssertionError as e:
            raise Exception('invalid ciphertext: invalid ephemeral pubkey')
        if not ecdsa.ecdsa.point_is_valid(generator_secp256k1, ephemeral_pubkey.x(), ephemeral_pubkey.y()):
            raise Exception('invalid ciphertext: invalid ephemeral pubkey')
        ecdh_key = point_to_ser(ephemeral_pubkey * self.privkey.secret_multiplier)
        key = hashlib.sha512(ecdh_key).digest()
        iv, key_e, key_m = key[0:16], key[16:32], key[32:]
        if mac != hmac.new(key_m, encrypted[:-32], hashlib.sha256).digest():
            raise InvalidPassword()
        return aes_decrypt_with_iv(key_e, iv, ciphertext)


###################################### BIP32 ##############################

random_seed = lambda n: "%032x"%ecdsa.util.randrange( pow(2,n) )
BIP32_PRIME = 0x80000000


def get_pubkeys_from_secret(secret):
    # public key
    private_key = ecdsa.SigningKey.from_string( secret, curve = SECP256k1 )
    public_key = private_key.get_verifying_key()
    K = public_key.to_string()
    K_compressed = GetPubKey(public_key.pubkey,True)
    return K, K_compressed


# Child private key derivation function (from master private key)
# k = master private key (32 bytes)
# c = master chain code (extra entropy for key derivation) (32 bytes)
# n = the index of the key we want to derive. (only 32 bits will be used)
# If n is negative (i.e. the 32nd bit is set), the resulting private key's
#  corresponding public key can NOT be determined without the master private key.
# However, if n is positive, the resulting private key's corresponding
#  public key can be determined without the master private key.
def CKD_priv(k, c, n):
    is_prime = n & BIP32_PRIME
    return _CKD_priv(k, c, bfh(rev_hex(int_to_hex(n,4))), is_prime)


def _CKD_priv(k, c, s, is_prime):
    order = generator_secp256k1.order()
    keypair = EC_KEY(k)
    cK = GetPubKey(keypair.pubkey,True)
    data = bytes([0]) + k + s if is_prime else cK + s
    I = hmac.new(c, data, hashlib.sha512).digest()
    k_n = number_to_string( (string_to_number(I[0:32]) + string_to_number(k)) % order , order )
    c_n = I[32:]
    return k_n, c_n

# Child public key derivation function (from public key only)
# K = master public key
# c = master chain code
# n = index of key we want to derive
# This function allows us to find the nth public key, as long as n is
#  non-negative. If n is negative, we need the master private key to find it.
def CKD_pub(cK, c, n):
    if n & BIP32_PRIME: raise
    return _CKD_pub(cK, c, bfh(rev_hex(int_to_hex(n,4))))

# helper function, callable with arbitrary string
def _CKD_pub(cK, c, s):
    order = generator_secp256k1.order()
    I = hmac.new(c, cK + s, hashlib.sha512).digest()
    curve = SECP256k1
    pubkey_point = string_to_number(I[0:32])*curve.generator + ser_to_point(cK)
    public_key = ecdsa.VerifyingKey.from_public_point( pubkey_point, curve = SECP256k1 )
    c_n = I[32:]
    cK_n = GetPubKey(public_key.pubkey,True)
    return cK_n, c_n


def xprv_header(xtype, *, net=None):
    if net is None:
        net = constants.net
    return bfh("%08x" % net.XPRV_HEADERS[xtype])


def xpub_header(xtype, *, net=None):
    if net is None:
        net = constants.net
    return bfh("%08x" % net.XPUB_HEADERS[xtype])


def serialize_xprv(xtype, c, k, depth=0, fingerprint=b'\x00'*4,
                   child_number=b'\x00'*4, *, net=None):
    xprv = xprv_header(xtype, net=net) \
           + bytes([depth]) + fingerprint + child_number + c + bytes([0]) + k
    return EncodeBase58Check(xprv)


def serialize_xpub(xtype, c, cK, depth=0, fingerprint=b'\x00'*4,
                   child_number=b'\x00'*4, *, net=None):
    xpub = xpub_header(xtype, net=net) \
           + bytes([depth]) + fingerprint + child_number + c + cK
    return EncodeBase58Check(xpub)


def deserialize_xkey(xkey, prv, *, net=None):
    if net is None:
        net = constants.net
    xkey = DecodeBase58Check(xkey)
    if len(xkey) != 78:
        raise BaseException('Invalid length')
    depth = xkey[4]
    fingerprint = xkey[5:9]
    child_number = xkey[9:13]
    c = xkey[13:13+32]
    header = int('0x' + bh2u(xkey[0:4]), 16)
    headers = net.XPRV_HEADERS if prv else net.XPUB_HEADERS
    if header not in headers.values():
        raise BaseException('Invalid xpub format', hex(header))
    xtype = list(headers.keys())[list(headers.values()).index(header)]
    n = 33 if prv else 32
    K_or_k = xkey[13+n:]
    return xtype, depth, fingerprint, child_number, c, K_or_k


def deserialize_xpub(xkey, *, net=None):
    return deserialize_xkey(xkey, False, net=net)

def deserialize_xprv(xkey, *, net=None):
    return deserialize_xkey(xkey, True, net=net)

def xpub_type(x):
    return deserialize_xpub(x)[0]


def is_xpub(text):
    try:
        deserialize_xpub(text)
        return True
    except:
        return False


def is_xprv(text):
    try:
        deserialize_xprv(text)
        return True
    except:
        return False


def xpub_from_xprv(xprv):
    xtype, depth, fingerprint, child_number, c, k = deserialize_xprv(xprv)
    K, cK = get_pubkeys_from_secret(k)
    return serialize_xpub(xtype, c, cK, depth, fingerprint, child_number)


def bip32_root(seed, xtype):
    I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    master_k = I[0:32]
    master_c = I[32:]
    K, cK = get_pubkeys_from_secret(master_k)
    xprv = serialize_xprv(xtype, master_c, master_k)
    xpub = serialize_xpub(xtype, master_c, cK)
    return xprv, xpub


def xpub_from_pubkey(xtype, cK):
    assert cK[0] in [0x02, 0x03]
    return serialize_xpub(xtype, b'\x00'*32, cK)


def bip32_derivation(s):
    assert s.startswith('m/')
    s = s[2:]
    for n in s.split('/'):
        if n == '': continue
        i = int(n[:-1]) + BIP32_PRIME if n[-1] == "'" else int(n)
        yield i

def is_bip32_derivation(x):
    try:
        [ i for i in bip32_derivation(x)]
        return True
    except :
        return False

def bip32_private_derivation(xprv, branch, sequence):
    assert sequence.startswith(branch)
    if branch == sequence:
        return xprv, xpub_from_xprv(xprv)
    xtype, depth, fingerprint, child_number, c, k = deserialize_xprv(xprv)
    sequence = sequence[len(branch):]
    for n in sequence.split('/'):
        if n == '': continue
        i = int(n[:-1]) + BIP32_PRIME if n[-1] == "'" else int(n)
        parent_k = k
        k, c = CKD_priv(k, c, i)
        depth += 1
    _, parent_cK = get_pubkeys_from_secret(parent_k)
    fingerprint = hash_160(parent_cK)[0:4]
    child_number = bfh("%08X"%i)
    K, cK = get_pubkeys_from_secret(k)
    xpub = serialize_xpub(xtype, c, cK, depth, fingerprint, child_number)
    xprv = serialize_xprv(xtype, c, k, depth, fingerprint, child_number)
    return xprv, xpub


def bip32_public_derivation(xpub, branch, sequence):
    xtype, depth, fingerprint, child_number, c, cK = deserialize_xpub(xpub)
    assert sequence.startswith(branch)
    sequence = sequence[len(branch):]
    for n in sequence.split('/'):
        if n == '': continue
        i = int(n)
        parent_cK = cK
        cK, c = CKD_pub(cK, c, i)
        depth += 1
    fingerprint = hash_160(parent_cK)[0:4]
    child_number = bfh("%08X"%i)
    return serialize_xpub(xtype, c, cK, depth, fingerprint, child_number)


def bip32_private_key(sequence, k, chain):
    for i in sequence:
        k, chain = CKD_priv(k, chain, i)
    return k
