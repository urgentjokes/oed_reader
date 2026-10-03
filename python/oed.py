#!/usr/bin/env python3
"""
OED (Oxford English Dictionary 2nd Edition CD-ROM) Python Interface
===================================================================

Provides direct, fast access to the contents of OED2.DAT from the mounted
Oxford English Dictionary 2nd Edition CD-ROM / ISO image.

Features:
- Automatic detection of mounted ISO volume (/Volumes/OED, /Volumes/OED2, etc.)
- Pure-Python decompression engine (zero external dependencies)
- Optional C acceleration (via ctypes/clang) for blazing-fast full exports
- Direct random-access block decompression (7,829 blocks of 32 KB)
- Sub-second binary search lookup over all ~300,000 words without prebuilding an index
- Structured SGML parsing (headword, pronunciation, part of speech, etymology, senses, quotations)
- Entity conversion (decoding OED SGML &... entities into Unicode)
- Interactive CLI with word lookup, search, random word, block inspector, full export, and shell
"""

import os
import sys
import glob
import re
import struct
import random
import argparse
import subprocess
import ctypes
import shutil
from typing import List, Dict, Optional, Tuple, Iterator, Any

# CD-ROM layout constants for OED2.DAT (sha1: 626fab18cc9a25feafcf4080901c834e3ca05af7)
OED_HEADER_START_OFFSET = 0x16701000
OED_BLOCKS_START_OFFSET = 0x16709000  # OED_HEADER_START_OFFSET + 0x8000
OED_BLOCKS_END_OFFSET   = 0x25bb1000  # 633,016,320 bytes
OED_BLOCK_SIZE          = 32768       # 32 KB per compressed block
OED_TOTAL_BLOCKS        = (OED_BLOCKS_END_OFFSET - OED_BLOCKS_START_OFFSET) // OED_BLOCK_SIZE  # 7,829 blocks

# Common SGML entities in OED2
SGML_ENTITIES = {
    '&oq.': "'", '&cq.': "'",
    '&odq.': '"', '&cdq.': '"',
    '&dd.': '...', '&mdash.': ' — ', '&ndash.': '–',
    '&es.': ' ', '&sm.': '', '&smm.': '',
    '&oe.': 'œ', '&ae.': 'æ',
    '&eacu.': 'é', '&egrave.': 'è', '&ecirc.': 'ê', '&euml.': 'ë',
    '&aacu.': 'á', '&agrave.': 'à', '&acirc.': 'â', '&auml.': 'ä',
    '&iacu.': 'í', '&igrave.': 'ì', '&icirc.': 'î', '&iuml.': 'ï',
    '&oacu.': 'ó', '&ograve.': 'ò', '&ocirc.': 'ô', '&ouml.': 'ö',
    '&uacu.': 'ú', '&ugrave.': 'ù', '&ucirc.': 'û', '&uuml.': 'ü',
    '&ccedil.': 'ç', '&ntilde.': 'ñ', '&th.': 'þ', '&dh.': 'ð',
    '&c.': '&c.', '&amp.': '&', '&ast.': '*',
    '&prime.': '′', '&sec.': '″',
    '&guacu.': '́',  # combining acute
}

# ANSI colors for terminal display
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ITALIC = "\033[3m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"


def decode_entities(text: str) -> str:
    """Decode OED-specific SGML entities into human-readable Unicode."""
    for ent, replacement in SGML_ENTITIES.items():
        text = text.replace(ent, replacement)
    # Remove any lingering SGML entity like &foo. or &foo;
    return re.sub(r'&[a-zA-Z0-9]+[.;]', '', text)


def strip_tags(text: str) -> str:
    """Remove XML/SGML tags and decode entities."""
    clean = re.sub(r'<[^>]+>', '', text)
    return decode_entities(clean).strip()


def normalize_headword(text: str) -> str:
    """Normalize a headword for alphabetical comparison (lowercase, strip tags/entities/punctuation)."""
    clean = re.sub(r'<[^>]+>', '', text)
    clean = re.sub(r'&[a-zA-Z0-9]+[.;]', '', clean)
    clean = re.sub(r'[^a-zA-Z0-9]', '', clean).lower()
    return re.sub(r'\d+$', '', clean)


def find_oed_dat_path(explicit_path: Optional[str] = None) -> str:
    """Auto-detect path to OED2.DAT."""
    if explicit_path and os.path.isfile(explicit_path):
        return os.path.abspath(explicit_path)

    for env_var in ("OED_DAT", "OED_PATH"):
        val = os.environ.get(env_var)
        if val:
            if os.path.isfile(val):
                return os.path.abspath(val)
            sub = os.path.join(val, "OED2.DAT")
            if os.path.isfile(sub):
                return os.path.abspath(sub)

    # Common mount points and current working directory
    quick_checks = [
        "/Volumes/OED2/OED2.DAT", "/Volumes/OED2/OED2.dat", "/Volumes/OED2/oed2.dat",
        "/Volumes/OED/OED2.DAT", "/Volumes/OED/OED2.dat", "/Volumes/OED/oed2.dat",
        "./OED2.DAT", "./OED2.dat", "./oed2.dat",
    ]
    for p in quick_checks:
        if os.path.isfile(p):
            return os.path.abspath(p)

    # Scan top-level /Volumes entries only (skipping Macintosh HD or root symlinks)
    if os.path.isdir("/Volumes"):
        for vol in os.listdir("/Volumes"):
            if "oed" in vol.lower():
                vol_dir = os.path.join("/Volumes", vol)
                if os.path.isdir(vol_dir):
                    for fn in os.listdir(vol_dir):
                        if fn.lower() in ("oed2.dat", "oed.dat"):
                            target = os.path.join(vol_dir, fn)
                            if os.path.isfile(target):
                                return os.path.abspath(target)

    raise FileNotFoundError(
        "Could not locate OED2.DAT! Please ensure the OED disk image is mounted, "
        "or specify the path with --dat-file /path/to/OED2.DAT."
    )


# ---------------------------------------------------------------------------
# Decompression Core
# ---------------------------------------------------------------------------

def _clong(b: bytearray, offset: int) -> int:
    return struct.unpack('>I', b[offset:offset+4])[0]


def decompress_block_python(raw_buf: bytes) -> bytes:
    """
    Decompress a single 32KB OED block in pure Python.
    Uses canonical prefix/Huffman decoding with dynamic dictionary.
    """
    buf = bytearray(raw_buf)
    if len(buf) < 0x200:
        return b""

    brktbl = _clong(buf, 8)
    symc = _clong(buf, 12)

    dict_offset = 0x10
    breaktable_offset = dict_offset + brktbl
    data_offset = breaktable_offset + 0x80 + 0x0100

    if data_offset >= len(buf):
        return b""

    # Enumerate symbol offsets
    symbols: List[int] = []
    data_idx = dict_offset
    for _ in range(symc):
        start = data_idx
        while data_idx < len(buf) and not (buf[data_idx] & 0x80):
            data_idx += 1
        if data_idx >= len(buf):
            break
        buf[data_idx] &= 0x7f
        data_idx += 1
        symbols.append(start)

    while data_idx < breaktable_offset and buf[data_idx] != 0:
        data_idx += 1
    symbols.append(data_idx)

    # Enumerate breakpoints (canonical Huffman prefix decode table)
    i = breaktable_offset
    num_brk = _clong(buf, i)
    breakpoints: List[Tuple[int, int, int]] = [(0, 0, 0)]
    next_val = _clong(buf, i + 4)
    for _ in range(num_brk - 1, 0, -1):
        i += 4
        this_val = next_val
        next_val = _clong(buf, i + 4)
        prev = breakpoints[-1]
        b0 = prev[1] * 2
        b1 = b0 + next_val
        b2 = prev[2] + this_val
        breakpoints.append((b0, b1, b2))

    # Bitstream decoding loop
    cur = data_offset
    mask = 0x80
    zeros = 0
    val = 0
    curbrk_idx = 0
    output_chunks: List[bytes] = []
    block_len = len(buf)
    num_symbols = len(symbols) - 1

    while cur < block_len:
        byte_val = buf[cur]
        while mask > 0:
            val <<= 1
            if byte_val & mask:
                val += 1
            b0, b1, b2 = breakpoints[curbrk_idx]
            if b0 <= val < b1:
                out = val + b2 - b0
                if out != 0:
                    zeros = 0
                    mask >>= 1
                    if out < num_symbols:
                        output_chunks.append(bytes(buf[symbols[out]:symbols[out+1]]))
                    val = 0
                    curbrk_idx = 0
                    continue
                else:
                    if zeros > 4:
                        return b''.join(output_chunks)
                    zeros += 1
                    val = 0
                    curbrk_idx = 0
            else:
                curbrk_idx += 1
            mask >>= 1
        cur += 1
        mask = 0x80

    return b''.join(output_chunks)


# Optional C Accelerator (compiled on-demand using clang if available)
_C_DECOMPRESSOR = None

def _get_c_decompressor():
    global _C_DECOMPRESSOR
    if _C_DECOMPRESSOR is not False and _C_DECOMPRESSOR is not None:
        return _C_DECOMPRESSOR

    cache_dir = os.path.expanduser("~/.cache/oed")
    so_path = os.path.join(cache_dir, "oed_decompress.so")

    if not os.path.isfile(so_path):
        c_code = r"""
#include <stdlib.h>
#include <string.h>

typedef unsigned char uchar;
typedef unsigned short ushort;
typedef unsigned int ulong;

static inline ulong clong(const uchar *src) {
    return ((ulong)src[0] << 24) | ((ulong)src[1] << 16) | ((ulong)src[2] << 8) | (ulong)src[3];
}

int oed_decompress_block(const uchar *in_buf, int in_len, uchar *out_buf, int max_out) {
    if (in_len < 0x200) return 0;
    uchar *buf = (uchar*)malloc(in_len);
    memcpy(buf, in_buf, in_len);

    ulong brktbl = clong(buf + 8);
    ulong symc = clong(buf + 12);
    ulong dict_offset = 0x10;
    ulong breaktable_offset = dict_offset + brktbl;
    ulong data_offset = breaktable_offset + 0x80 + 0x0100;

    if (data_offset >= (ulong)in_len) { free(buf); return 0; }

    ushort *symbols = (ushort*)malloc((symc + 1) * sizeof(ushort));
    uchar *data = buf + dict_offset;
    for (ulong s = 0; s < symc; s++) {
        symbols[s] = data - buf;
        while (~*data & 0x80) data++;
        *data = *data & 0x7f;
        data++;
    }
    while (data < buf + breaktable_offset && *data != 0) data++;
    symbols[symc] = data - buf;

    ulong num_brk = clong(buf + breaktable_offset);
    ulong *breakpoints = (ulong*)malloc((num_brk + 1) * 3 * sizeof(ulong));
    ulong *dst = breakpoints;
    *dst++ = 0; *dst++ = 0; *dst++ = 0;
    uchar *bp_ptr = buf + breaktable_offset;
    ulong next_val = clong(bp_ptr + 4);
    for (ulong c = num_brk - 1; c; c--) {
        bp_ptr += 4;
        ulong this_val = next_val;
        next_val = clong(bp_ptr + 4);
        *dst = *(dst - 2) * 2; dst++;
        *dst = *(dst - 1) + next_val; dst++;
        *dst = *(dst - 3) + this_val; dst++;
    }

    uchar *cur = buf + data_offset;
    uchar mask = 0x80;
    int zeros = 0;
    ulong val = 0;
    ulong *curbrk = breakpoints;
    int out_pos = 0;

    while (cur < buf + in_len) {
        for (; mask; mask >>= 1) {
            val <<= 1;
            if (*cur & mask) val++;
            if (val >= curbrk[0] && val < curbrk[1]) {
                val = val + curbrk[2] - curbrk[0];
                if (val) {
                    zeros = 0;
                    if (val < symc) {
                        int sym_len = symbols[val + 1] - symbols[val];
                        if (out_pos + sym_len < max_out) {
                            memcpy(out_buf + out_pos, buf + symbols[val], sym_len);
                            out_pos += sym_len;
                        }
                    }
                    val = 0;
                    curbrk = breakpoints;
                    continue;
                } else {
                    if (zeros > 4) {
                        free(breakpoints);
                        free(symbols);
                        free(buf);
                        return out_pos;
                    }
                    zeros++;
                }
                val = 0;
                curbrk = breakpoints;
            } else {
                curbrk += 3;
            }
        }
        cur++;
        mask = 0x80;
    }

    free(breakpoints);
    free(symbols);
    free(buf);
    return out_pos;
}
"""
        try:
            os.makedirs(cache_dir, exist_ok=True)
            c_src = os.path.join(cache_dir, "oed_decompress.c")
            with open(c_src, "w") as f:
                f.write(c_code)
            compiler = "clang" if os.path.exists("/usr/bin/clang") else "gcc"
            subprocess.run(
                [compiler, "-O3", "-shared", "-fPIC", c_src, "-o", so_path],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            _C_DECOMPRESSOR = False
            return None

    try:
        lib = ctypes.CDLL(so_path)
        lib.oed_decompress_block.argtypes = [
            ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int
        ]
        lib.oed_decompress_block.restype = ctypes.c_int
        _C_DECOMPRESSOR = lib
        return lib
    except Exception:
        _C_DECOMPRESSOR = False
        return None


def decompress_block(raw_buf: bytes, use_c_acceleration: bool = True) -> str:
    """Decompress a single 32KB block and return decoded latin1/ASCII text."""
    if use_c_acceleration:
        lib = _get_c_decompressor()
        if lib:
            out_buf = ctypes.create_string_buffer(256 * 1024)
            ret_len = lib.oed_decompress_block(raw_buf, len(raw_buf), out_buf, 256 * 1024)
            if ret_len > 0:
                return out_buf.raw[:ret_len].decode('latin1', errors='replace')

    raw = decompress_block_python(raw_buf)
    return raw.decode('latin1', errors='replace')


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

def oed_phonetic_to_ipa(s: str) -> str:
    """Convert OED ASCII phonetic alphabet to standard Unicode IPA."""
    mapping = {
        '@': 'ə', '"': 'ˈ', '%': 'ˌ', ':': 'ː',
        'T': 'θ', 'D': 'ð', 'S': 'ʃ', 'Z': 'ʒ', 'N': 'ŋ',
        'V': 'ʌ', 'Q': 'ɒ', 'O': 'ɔ', 'E': 'ɛ', 'I': 'ɪ',
        'U': 'ʊ', 'A': 'æ', '3': 'ɜ',
    }
    return ''.join(mapping.get(c, c) for c in s)


def render_box(title: str, lines: List[str], width: int = 78, color_border: str = "") -> str:
    """Render text lines inside a rounded ASCII/Unicode box with an optional titled top bar."""
    reset = RESET if color_border else ""
    bold = BOLD if color_border else ""
    ansi_re = re.compile(r'\033\[[0-9;]*m')

    t_vis = ansi_re.sub('', title)
    t_len = len(t_vis) + 2 if title else 0
    dash_len = max(0, width - 4 - t_len)
    top_bar = (
        f"{color_border}╭─{reset} {bold}{title}{reset} {color_border}{'─' * dash_len}╮{reset}"
        if title else
        f"{color_border}╭{'─' * (width - 2)}╮{reset}"
    )

    result = [top_bar]
    inner_width = width - 4
    for raw_line in lines:
        vis_len = len(ansi_re.sub('', raw_line))
        pad = max(0, inner_width - vis_len)
        result.append(f"{color_border}│{reset} {raw_line}{' ' * pad} {color_border}│{reset}")
    result.append(f"{color_border}╰{'─' * (width - 2)}╯{reset}")
    return '\n'.join(result)


class OEDEntry:
    """Represents a structured entry in the Oxford English Dictionary."""
    def __init__(self, raw_sgml: str):
        self.raw_sgml = raw_sgml.strip()
        self.headword: str = ""
        self.homograph: Optional[int] = None
        self.pronunciation: Optional[str] = None
        self.ipa_pronunciation: Optional[str] = None
        self.part_of_speech: Optional[str] = None
        self.variant_forms: List[str] = []
        self.etymology: Optional[str] = None
        self.senses: List[Dict[str, Any]] = []
        self.quotations: List[Dict[str, str]] = []
        self._parse()

    def _parse(self):
        # Headword & Homograph
        hw_match = re.search(r'<hw>(.*?)</hw>', self.raw_sgml)
        if hw_match:
            raw_hw = hw_match.group(1)
            hm_match = re.search(r'<hm>(.*?)</hm>', raw_hw)
            if hm_match:
                try:
                    self.homograph = int(hm_match.group(1))
                except ValueError:
                    pass
            self.headword = strip_tags(raw_hw)

        # Pronunciation
        pr_match = re.search(r'<pr>(.*?)</pr>', self.raw_sgml)
        if pr_match:
            self.pronunciation = strip_tags(pr_match.group(1))
            self.ipa_pronunciation = oed_phonetic_to_ipa(self.pronunciation)

        # Part of speech: check headword group <hg> first, or before <etym>
        hg_match = re.search(r'<hg>(.*?)</hg>', self.raw_sgml, re.DOTALL)
        if hg_match:
            ps_match = re.search(r'<ps>(.*?)</ps>', hg_match.group(1))
            if ps_match:
                self.part_of_speech = strip_tags(ps_match.group(1))

        if not self.part_of_speech:
            hg_end = self.raw_sgml.find('</hg>')
            if hg_end != -1:
                etym_pos = self.raw_sgml.find('<etym>')
                limit = etym_pos if etym_pos != -1 else (hg_end + 100)
                pre_slice = self.raw_sgml[hg_end:limit]
                ps_match = re.search(r'<ps>(.*?)</ps>', pre_slice)
                if ps_match:
                    self.part_of_speech = strip_tags(ps_match.group(1))

        # Variant forms
        for vf_match in re.finditer(r'<vfl>(.*?)</vfl>', self.raw_sgml, re.DOTALL):
            vf_clean = strip_tags(vf_match.group(1))
            if vf_clean and vf_clean not in self.variant_forms:
                self.variant_forms.append(vf_clean)

        # Etymology
        etym_match = re.search(r'<etym>(.*?)</etym>', self.raw_sgml, re.DOTALL)
        if etym_match:
            self.etymology = strip_tags(etym_match.group(1))

        # Structured Senses (cleaning out embedded quotation blocks)
        senses_found = list(re.finditer(r'<s4([^>]*)>(.*?)</s4>', self.raw_sgml, re.DOTALL))
        if senses_found:
            for s_match in senses_found:
                attrs = s_match.group(1)
                num_m = re.search(r'\bnum=([^\s>]+)', attrs)
                num = (num_m.group(1).strip('"\'')) if num_m else ""
                inner = s_match.group(2)
                # Strip quotation blocks
                clean = re.sub(r'<qp>.*?</qp>', '', inner, flags=re.DOTALL)
                clean = re.sub(r'<pqp>.*?</pqp>', '', clean, flags=re.DOTALL)
                clean = re.sub(r'<q>.*?</q>', '', clean, flags=re.DOTALL)

                s6_list = list(re.finditer(r'<s6([^>]*)>(.*?)</s6>', clean, re.DOTALL))
                if s6_list:
                    lead_text = strip_tags(clean[:s6_list[0].start()]).strip()
                    sub_senses = []
                    for s6 in s6_list:
                        s6_attrs = s6.group(1)
                        s6_num_m = re.search(r'\bnum=([^\s>]+)', s6_attrs)
                        s6_num = (s6_num_m.group(1).strip('"\'')) if s6_num_m else ""
                        s6_body = strip_tags(s6.group(2)).strip()
                        if s6_body:
                            sub_senses.append({"num": s6_num, "definition": s6_body})
                    self.senses.append({
                        "num": num,
                        "lead": lead_text,
                        "sub_senses": sub_senses,
                        "definition": lead_text
                    })
                else:
                    body = strip_tags(clean).strip()
                    if body:
                        self.senses.append({
                            "num": num,
                            "lead": "",
                            "sub_senses": [],
                            "definition": body
                        })
        else:
            # Fallback if no <s4>
            for s_match in re.finditer(r'<s(\d+)([^>]*)>(.*?)</s\1>', self.raw_sgml, re.DOTALL):
                attrs = s_match.group(2)
                num_m = re.search(r'\bnum=([^\s>]+)', attrs)
                num = (num_m.group(1).strip('"\'')) if num_m else ""
                inner = s_match.group(3)
                clean = re.sub(r'<qp>.*?</qp>', '', inner, flags=re.DOTALL)
                clean = re.sub(r'<pqp>.*?</pqp>', '', clean, flags=re.DOTALL)
                clean = re.sub(r'<q>.*?</q>', '', clean, flags=re.DOTALL)
                body = strip_tags(clean).strip()
                if body:
                    self.senses.append({
                        "num": num,
                        "lead": "",
                        "sub_senses": [],
                        "definition": body
                    })

        # Fallback for <ve> (variant entries) or definition text outside <s4>/<s...> tags
        if not self.senses:
            clean = re.sub(r'<hg>.*?</hg>', '', self.raw_sgml, flags=re.DOTALL)
            clean = re.sub(r'<etym>.*?</etym>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<qp>.*?</qp>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<pqp>.*?</pqp>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<q>.*?</q>', '', clean, flags=re.DOTALL)
            clean = re.sub(r'<pr>.*?</pr>', '', clean, flags=re.DOTALL)
            body = strip_tags(clean).strip()
            body = re.sub(r'^[\s,.;:\-]+', '', body).strip()
            if body:
                self.senses.append({
                    "num": "",
                    "lead": "",
                    "sub_senses": [],
                    "definition": body
                })

        # Historical Quotations
        for q_match in re.finditer(r'<q>(.*?)</q>', self.raw_sgml, re.DOTALL):
            q_raw = q_match.group(1)
            qd = re.search(r'<qd>(.*?)</qd>', q_raw)
            qa = re.search(r'<a>(.*?)</a>', q_raw)
            qw = re.search(r'<w>(.*?)</w>', q_raw)
            qt = re.search(r'<qt>(.*?)</qt>', q_raw)
            self.quotations.append({
                "date": strip_tags(qd.group(1)) if qd else "",
                "author": strip_tags(qa.group(1)) if qa else "",
                "work": strip_tags(qw.group(1)) if qw else "",
                "text": strip_tags(qt.group(1)) if qt else ""
            })

    def format_terminal(self, color: bool = True, max_width: Optional[int] = None, all_quotes: bool = False) -> str:
        """Format entry into visually appealing boxed blocks for terminal viewing."""
        import shutil
        import textwrap

        term_width = shutil.get_terminal_size((80, 24)).columns
        width = max_width or min(92, max(64, term_width - 4))
        inner_w = width - 4

        b = BOLD if color else ""
        c = CYAN if color else ""
        g = GREEN if color else ""
        y = YELLOW if color else ""
        m = MAGENTA if color else ""
        dim = DIM if color else ""
        it = ITALIC if color else ""
        r = RESET if color else ""

        box_blocks = []

        # 1. HEADER BOX
        h_title = f"{b}{c}{self.headword.upper()}{r}"
        if self.homograph:
            h_title += f" {b}[{self.homograph}]{r}"

        right_info = []
        if self.ipa_pronunciation:
            right_info.append(f"{dim}/{self.ipa_pronunciation}/{r}")
        if self.part_of_speech:
            right_info.append(f"{it}{y}{self.part_of_speech}{r}")

        r_str = "  ".join(right_info)
        ansi_re = re.compile(r'\033\[[0-9;]*m')
        vis_h = len(ansi_re.sub('', h_title))
        vis_r = len(ansi_re.sub('', r_str))

        header_lines = []
        if vis_h + vis_r + 2 <= inner_w:
            space = " " * (inner_w - vis_h - vis_r)
            header_lines.append(f"{h_title}{space}{r_str}")
        else:
            header_lines.append(h_title)
            if r_str:
                header_lines.append(r_str)

        if self.variant_forms:
            vf_str = f"{dim}Variants: {', '.join(self.variant_forms)}{r}"
            header_lines.append(vf_str)

        box_blocks.append(render_box("", header_lines, width=width, color_border=CYAN if color else ""))

        # 2. ETYMOLOGY BOX
        if self.etymology:
            etym_wrapped = textwrap.wrap(self.etymology, width=inner_w - 2)
            etym_lines = [f"{it}{line}{r}" for line in etym_wrapped]
            box_blocks.append(render_box(f"{y}ETYMOLOGY{r}", etym_lines, width=width, color_border=YELLOW if color else ""))

        # 3. DEFINITIONS BOX
        if self.senses:
            def_lines = []
            for s_idx, sense in enumerate(self.senses):
                num = sense.get("num", "")
                prefix = f"{b}{g}{num}.{r} " if num else f"{g}•{r} "
                indent_prefix = "   " if num else "  "

                sub_senses = sense.get("sub_senses", [])
                lead = sense.get("lead", "")

                if sub_senses:
                    if lead:
                        w_lead = textwrap.wrap(lead, width=inner_w - 4)
                        def_lines.append(f"{prefix}{w_lead[0]}")
                        for w in w_lead[1:]:
                            def_lines.append(f"{indent_prefix}{w}")
                    elif num:
                        def_lines.append(f"{b}{g}{num}.{r}")

                    for s6 in sub_senses:
                        s6_num = s6.get("num", "")
                        s6_body = s6.get("definition", "")
                        s6_pref = f"  {y}({s6_num}){r} " if s6_num else "  • "
                        w_s6 = textwrap.wrap(s6_body, width=inner_w - 7)
                        if w_s6:
                            def_lines.append(f"{s6_pref}{w_s6[0]}")
                            for w in w_s6[1:]:
                                def_lines.append(f"      {w}")
                else:
                    body = sense.get("definition", "")
                    if body:
                        w_body = textwrap.wrap(body, width=inner_w - 4)
                        if w_body:
                            def_lines.append(f"{prefix}{w_body[0]}")
                            for w in w_body[1:]:
                                def_lines.append(f"{indent_prefix}{w}")

                if s_idx < len(self.senses) - 1:
                    def_lines.append("")

            if def_lines:
                box_blocks.append(render_box(f"{g}DEFINITIONS{r}", def_lines, width=width, color_border=GREEN if color else ""))

        # 4. HISTORICAL QUOTATIONS BOX
        if self.quotations:
            q_lines = []
            total_q = len(self.quotations)
            display_count = total_q if all_quotes else min(6, total_q)
            for q_idx, q in enumerate(self.quotations[:display_count]):
                src_parts = []
                if q['date']:
                    src_parts.append(f"{b}{q['date']}{r}")
                if q['author']:
                    src_parts.append(f"{m}{q['author']}{r}")
                if q['work']:
                    src_parts.append(f"{it}{dim}{q['work']}{r}")
                src_str = ", ".join(src_parts) if src_parts else "Source"

                q_lines.append(f"{c}•{r} [{src_str}]")
                q_text = f"\"{q['text']}\""
                w_quote = textwrap.wrap(q_text, width=inner_w - 4)
                for w in w_quote:
                    q_lines.append(f"   {w}")
                if q_idx < display_count - 1:
                    q_lines.append("")

            if total_q > display_count and not all_quotes:
                q_lines.append("")
                q_lines.append(f"{dim}... and {total_q - display_count} more historical quotations (add --all to view all, or --raw for SGML){r}")

            q_title = f"{m}HISTORICAL QUOTATIONS{r} {dim}({total_q} recorded){r}"
            box_blocks.append(render_box(q_title, q_lines, width=width, color_border=MAGENTA if color else ""))

        return "\n\n".join(box_blocks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "headword": self.headword,
            "homograph": self.homograph,
            "pronunciation": self.pronunciation,
            "ipa_pronunciation": self.ipa_pronunciation,
            "part_of_speech": self.part_of_speech,
            "variant_forms": self.variant_forms,
            "etymology": self.etymology,
            "senses": self.senses,
            "quotations": self.quotations,
            "raw_sgml": self.raw_sgml,
        }

    def __repr__(self) -> str:
        return f"<OEDEntry '{self.headword}' ({self.part_of_speech or 'n/a'})>"


# ---------------------------------------------------------------------------
# OED Reader
# ---------------------------------------------------------------------------

class OEDReader:
    """Interface to read, search, and decompress data from OED2.DAT."""
    def __init__(self, dat_path: Optional[str] = None):
        self.dat_path = find_oed_dat_path(dat_path)
        self.file_size = os.path.getsize(self.dat_path)
        self.blocks_start = OED_BLOCKS_START_OFFSET
        self.total_blocks = OED_TOTAL_BLOCKS
        self._f = open(self.dat_path, "rb")

    def close(self):
        if self._f:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def read_block_raw(self, block_idx: int) -> bytes:
        """Read 32KB of raw compressed data for block_idx."""
        if not (0 <= block_idx < self.total_blocks):
            raise IndexError(f"Block index {block_idx} out of range [0, {self.total_blocks})")
        self._f.seek(self.blocks_start + block_idx * OED_BLOCK_SIZE)
        return self._f.read(OED_BLOCK_SIZE)

    def decompress_block(self, block_idx: int) -> str:
        """Decompress block block_idx and return SGML string."""
        raw = self.read_block_raw(block_idx)
        return decompress_block(raw)

    def get_block_headwords(self, block_idx: int) -> List[str]:
        """Extract clean headwords present in block_idx."""
        text = self.decompress_block(block_idx)
        raw_hws = re.findall(r'<hw>(.*?)</hw>', text)
        return [strip_tags(h) for h in raw_hws if strip_tags(h)]

    def lookup(self, word: str) -> List[OEDEntry]:
        """
        Fast binary search for a headword across the 7,829 compressed blocks.
        Completes in under 0.4 seconds without needing an index.
        """
        norm_target = normalize_headword(word)
        if not norm_target:
            return []

        low, high = 0, self.total_blocks - 1
        target_block = -1

        # Binary search over alphabetically ordered blocks
        while low <= high:
            mid = (low + high) // 2
            text = self.decompress_block(mid)
            raw_hws = re.findall(r'<hw>(.*?)</hw>', text)
            hws = [normalize_headword(h) for h in raw_hws if normalize_headword(h)]

            # If this block contains an entry so huge it has no <hw> tag, walk backwards
            if not hws:
                prev = mid - 1
                while prev >= 0 and not hws:
                    p_text = self.decompress_block(prev)
                    p_hws = [normalize_headword(h) for h in re.findall(r'<hw>(.*?)</hw>', p_text) if normalize_headword(h)]
                    if p_hws:
                        hws = [p_hws[-1]]
                    prev -= 1

            if not hws:
                break

            first_hw, last_hw = hws[0], hws[-1]
            if first_hw <= norm_target <= last_hw or norm_target in hws:
                target_block = mid
                break
            elif norm_target < first_hw:
                high = mid - 1
            else:
                low = mid + 1

        if target_block == -1:
            target_block = max(0, min(self.total_blocks - 1, high))

        # Check target_block and adjacent blocks to handle boundaries
        start_blk = max(0, target_block - 1)
        end_blk = min(self.total_blocks, target_block + 3)
        combined_text = "".join(self.decompress_block(b) for b in range(start_blk, end_blk))

        matches = []
        for m in re.finditer(r'<(?:e|ve)(?:\s+[^>]*)?>(.*?)</(?:e|ve)>', combined_text, re.DOTALL):
            entry_body = m.group(0)
            hws = re.findall(r'<hw>(.*?)</hw>', entry_body)
            for hw in hws:
                if normalize_headword(hw) == norm_target:
                    matches.append(OEDEntry(entry_body))
                    break

        return matches

    def search_prefix(self, prefix: str, max_results: int = 20) -> List[str]:
        """Search for headwords matching a given prefix."""
        norm_prefix = normalize_headword(prefix)
        # Find starting block via binary search
        low, high = 0, self.total_blocks - 1
        start_block = 0
        while low <= high:
            mid = (low + high) // 2
            text = self.decompress_block(mid)
            raw_hws = re.findall(r'<hw>(.*?)</hw>', text)
            hws = [normalize_headword(h) for h in raw_hws if normalize_headword(h)]
            if not hws:
                high = mid - 1
                continue
            if hws[-1] < norm_prefix:
                low = mid + 1
            else:
                start_block = mid
                high = mid - 1

        results = []
        seen = set()
        for b in range(start_block, min(self.total_blocks, start_block + 15)):
            hws = self.get_block_headwords(b)
            for h in hws:
                norm_h = normalize_headword(h)
                if norm_h.startswith(norm_prefix):
                    if norm_h not in seen:
                        seen.add(norm_h)
                        results.append(h)
                        if len(results) >= max_results:
                            return results
                elif not h.strip().startswith('-') and norm_h[:len(norm_prefix)] > norm_prefix and results:
                    return results
        return results

    def get_headword_index(self, force_rebuild: bool = False) -> List[Tuple[str, str]]:
        """
        Load or build the comprehensive headword index (norm_hw, display_hw).
        Cached to ~/.cache/oed/headwords.tsv for instantaneous sub-millisecond lookups.
        """
        cache_dir = os.path.expanduser("~/.cache/oed")
        cache_file = os.path.join(cache_dir, "headwords.tsv")

        if not force_rebuild and os.path.isfile(cache_file):
            try:
                entries = []
                with open(cache_file, "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.rstrip("\n").split("\t", 1)
                        if len(parts) == 2:
                            entries.append((parts[0], parts[1]))
                if entries:
                    return entries
            except Exception:
                pass

        # Build index from all blocks
        print(f"{CYAN}Indexing headwords across {self.total_blocks:,} blocks (one-time setup, ~3s)...{RESET}", end="", flush=True)
        os.makedirs(cache_dir, exist_ok=True)
        hw_regex = re.compile(r'<hw>(.*?)</hw>')
        hm_regex = re.compile(r'<hm>.*?</hm>')
        by_clean: Dict[str, Tuple[str, str]] = {}

        for b in range(self.total_blocks):
            text = self.decompress_block(b)
            for hw in hw_regex.findall(text):
                clean_no_hm = hm_regex.sub("", hw)
                clean = strip_tags(clean_no_hm).strip()
                norm = normalize_headword(clean_no_hm)
                if not norm or clean.startswith("-"):
                    continue
                lower_clean = clean.lower()
                if lower_clean not in by_clean:
                    by_clean[lower_clean] = (norm, clean)

        sorted_entries = sorted(by_clean.values(), key=lambda item: (item[0], item[1].lower()))
        with open(cache_file, "w", encoding="utf-8") as f:
            for norm, clean in sorted_entries:
                f.write(f"{norm}\t{clean}\n")
        print(f" {GREEN}Done! ({len(sorted_entries):,} words cached){RESET}")
        return sorted_entries

    def search_suffix(
        self,
        suffix: str,
        single_only: bool = False,
        sort_by_length: bool = False,
        max_results: Optional[int] = None
    ) -> List[str]:
        """
        Search for headwords ending with a specific letter sequence / suffix.
        Returns matching headwords.
        """
        norm_suffix = normalize_headword(suffix)
        clean_suffix = suffix.strip().lower()
        if not norm_suffix and not clean_suffix:
            return []

        index = self.get_headword_index()
        matches = []
        for norm, clean in index:
            if clean.startswith("-") or clean.endswith("-"):
                continue
            lower_clean = clean.lower()
            if (norm_suffix and norm.endswith(norm_suffix)) or lower_clean.endswith(clean_suffix):
                if single_only and (" " in clean or "-" in clean):
                    continue
                matches.append(clean)

        # Deduplicate preserving case preference
        seen = set()
        deduped = []
        for w in matches:
            low = w.lower()
            if low not in seen:
                seen.add(low)
                deduped.append(w)

        if sort_by_length:
            deduped.sort(key=lambda s: (len(s), s.lower()))
        else:
            deduped.sort(key=lambda s: (s.lower(), s))

        if max_results and max_results > 0:
            return deduped[:max_results]
        return deduped

    def search_text(
        self,
        query: str,
        progress_callback: Optional[Any] = None
    ) -> List[Tuple[str, str, int]]:
        """
        Search for a substring within the entire text of the dictionary across all 7,829 blocks.
        Returns a list of tuples: (headword_display, entry_sgml, block_idx).
        """
        query_clean = query.strip()
        if not query_clean:
            return []

        query_lower = query_clean.lower()
        matches: List[Tuple[str, str, int]] = []
        entry_re = re.compile(r'<(?:e|ve)(?:\s+[^>]*)?>.*?</(?:e|ve)>', re.DOTALL)
        hw_re = re.compile(r'<hw>(.*?)</hw>')

        for b in range(self.total_blocks):
            if progress_callback and b % 250 == 0:
                progress_callback(b, self.total_blocks)

            text = self.decompress_block(b)
            if query_lower not in text.lower():
                continue

            for m in entry_re.finditer(text):
                eb = m.group(0)
                if query_lower in eb.lower():
                    hws = hw_re.findall(eb)
                    hw_display = ", ".join(strip_tags(h) for h in hws) if hws else "unknown"
                    matches.append((hw_display, eb, b))

        if progress_callback:
            progress_callback(self.total_blocks, self.total_blocks)

        return matches


    def random_entry(self) -> OEDEntry:
        """Return a random entry from the dictionary."""
        for _ in range(10):
            block_idx = random.randint(0, self.total_blocks - 1)
            text = self.decompress_block(block_idx)
            entries = list(re.finditer(r'<(?:e|ve)(?:\s+[^>]*)?>(.*?)</(?:e|ve)>', text, re.DOTALL))
            if entries:
                choice = random.choice(entries)
                return OEDEntry(choice.group(0))
        raise RuntimeError("Failed to sample random entry")

    def iter_blocks(self, start: int = 0, end: Optional[int] = None) -> Iterator[Tuple[int, str]]:
        """Yield (block_idx, decompressed_sgml) for blocks."""
        if end is None:
            end = self.total_blocks
        for b in range(start, end):
            yield (b, self.decompress_block(b))

    def export_sgml(self, output_path: str, start: int = 0, end: Optional[int] = None, callback=None):
        """Decompress and write complete SGML stream to disk."""
        if end is None:
            end = self.total_blocks
        with open(output_path, "w", encoding="utf-8", errors="replace") as out:
            for b, text in self.iter_blocks(start, end):
                out.write(text)
                if callback:
                    callback(b - start + 1, end - start)


# ---------------------------------------------------------------------------
# Terminal Pager ('more' behavior)
# ---------------------------------------------------------------------------

def paginate(text: str, enabled: bool = True):
    """
    Display text using a pager like bash 'more'.
    Pauses after each screenful, allowing the user to advance by screen (Space),
    by line (Enter/Return), or quit (q/Q).
    """
    if not enabled or not sys.stdout.isatty():
        print(text)
        return

    lines = text.split("\n")
    import shutil
    term_size = shutil.get_terminal_size((80, 24))
    page_size = max(4, term_size.lines - 2)

    if len(lines) <= page_size:
        print(text)
        return

    tty_file = None
    try:
        if sys.stdin.isatty():
            tty_fd = sys.stdin.fileno()
        else:
            tty_file = open("/dev/tty", "r")
            tty_fd = tty_file.fileno()
    except Exception:
        print(text)
        return

    import termios
    import tty

    i = 0
    try:
        while i < len(lines):
            chunk = lines[i:i + page_size]
            for line in chunk:
                print(line)
            i += len(chunk)
            if i >= len(lines):
                break

            pct = int((i / len(lines)) * 100)
            prompt = f"\033[7m--More-- ({pct}% - [Space]: next page, [Enter]: line, [q]: quit)\033[0m"
            sys.stdout.write(prompt)
            sys.stdout.flush()

            # Read single keypress in raw mode
            old_settings = termios.tcgetattr(tty_fd)
            try:
                tty.setraw(tty_fd)
                ch = os.read(tty_fd, 1).decode("utf-8", errors="ignore")
            finally:
                termios.tcsetattr(tty_fd, termios.TCSADRAIN, old_settings)

            # Clear prompt line
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

            if ch in ("q", "Q", "\x03", "\x04"):  # q, Q, Ctrl+C, Ctrl+D
                break
            elif ch in ("\r", "\n", "j"):
                page_size = 1
            else:
                # Space or other keys: advance full page
                term_size = shutil.get_terminal_size((80, 24))
                page_size = max(4, term_size.lines - 2)
    finally:
        if tty_file:
            tty_file.close()


# ---------------------------------------------------------------------------
# Command Line Interface
# ---------------------------------------------------------------------------

def cli_info(reader: OEDReader):
    print(f"\n{BOLD}=== Oxford English Dictionary 2nd Edition (OED2) ==={RESET}")
    print(f"  {CYAN}Data file:{RESET}           {reader.dat_path}")
    print(f"  {CYAN}File size:{RESET}           {reader.file_size:,} bytes ({reader.file_size / (1024*1024):.1f} MB)")
    print(f"  {CYAN}Compressed range:{RESET}    0x{OED_BLOCKS_START_OFFSET:08x} -> 0x{OED_BLOCKS_END_OFFSET:08x}")
    print(f"  {CYAN}Total 32KB blocks:{RESET}   {reader.total_blocks:,}")
    c_accel = bool(_get_c_decompressor())
    print(f"  {CYAN}C acceleration:{RESET}      {'Enabled' if c_accel else 'Disabled (using pure Python)'}")
    print(f"  {CYAN}Sample lookup:{RESET}       try: python3 oed.py lookup computer\n")


def cli_lookup(reader: OEDReader, word: str, raw: bool = False, all_quotes: bool = False, use_pager: bool = True):
    entries = reader.lookup(word)
    if not entries:
        print(f"No exact entry found for '{word}'.")
        suggestions = reader.search_prefix(word, max_results=5)
        if suggestions:
            print(f"Did you mean: {', '.join(suggestions)}?")
        return

    blocks = []
    for entry in entries:
        if raw:
            blocks.append(entry.raw_sgml)
        else:
            blocks.append(entry.format_terminal(all_quotes=all_quotes))

    output = "\n\n" + ("\n" + "—" * 60 + "\n\n").join(blocks)
    paginate(output, enabled=use_pager)


def format_word_grid(words: List[str], title: str, width: Optional[int] = None) -> str:
    """Format a list of words into a multi-column boxed grid."""
    if not words:
        return "No words found."
    if width is None:
        term_width = shutil.get_terminal_size((80, 24)).columns
        width = max(60, min(100, term_width))

    inner_width = width - 4
    max_len = max(len(w) for w in words)
    col_width = max(max_len + 2, 14)
    num_cols = max(1, min(len(words), inner_width // col_width))
    actual_col_width = inner_width // num_cols
    num_rows = (len(words) + num_cols - 1) // num_cols

    grid_lines = [""]
    for r in range(num_rows):
        row_cells = []
        for c in range(num_cols):
            idx = c * num_rows + r
            if idx < len(words):
                w = words[idx]
                row_cells.append(w.ljust(actual_col_width))
        grid_lines.append("".join(row_cells).rstrip())
    grid_lines.append("")
    return render_box(title, grid_lines, width=width, color_border=CYAN)


def cli_end_search(
    reader: OEDReader,
    suffix: str,
    single_only: bool = False,
    sort_len: bool = False,
    limit: Optional[int] = None,
    use_pager: bool = True
):
    results = reader.search_suffix(suffix, single_only=single_only, sort_by_length=sort_len)
    total_found = len(results)
    if not results:
        msg = f"No headwords found ending with '{suffix}'"
        if single_only:
            msg += " (single words only)"
        print(f"\n{msg}.\n")
        return

    displayed = results[:limit] if limit and limit > 0 else results
    suffix_desc = f'"{suffix}"'
    if single_only:
        suffix_desc += " [single words only]"

    title = f"WORDS ENDING WITH {suffix_desc} ({total_found:,} match{'es' if total_found != 1 else ''})"
    if limit and limit < total_found:
        title += f" [showing first {limit}]"

    output = "\n" + format_word_grid(displayed, title=title)
    paginate(output, enabled=use_pager)


def wrap_ansi(text: str, width: int) -> List[str]:
    """Wrap text to a maximum visible width, preserving ANSI escape codes."""
    ansi_re = re.compile(r'\033\[[0-9;]*m')
    words = text.split(' ')
    lines = []
    cur_line = []
    cur_vis_len = 0
    for w in words:
        w_vis = len(ansi_re.sub('', w))
        if cur_vis_len + (1 if cur_line else 0) + w_vis <= width:
            cur_line.append(w)
            cur_vis_len += (1 if cur_vis_len > 0 else 0) + w_vis
        else:
            if cur_line:
                lines.append(' '.join(cur_line))
            cur_line = [w]
            cur_vis_len = w_vis
    if cur_line:
        lines.append(' '.join(cur_line))
    return lines


def extract_snippets(entry_sgml: str, query: str, max_snippets: int = 2, window: int = 45) -> List[str]:
    """Extract context snippets around occurrences of query within an entry."""
    clean = strip_tags(entry_sgml)
    clean_lower = clean.lower()
    q_lower = query.lower()
    snippets = []
    pos = 0
    while len(snippets) < max_snippets:
        idx = clean_lower.find(q_lower, pos)
        if idx == -1:
            break
        start = max(0, idx - window)
        end = min(len(clean), idx + len(query) + window)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(clean) else ""
        left = " ".join(clean[start:idx].split())
        match_str = clean[idx:idx + len(query)]
        right = " ".join(clean[idx + len(query):end].split())
        snip = f"{prefix}{left} {BOLD}{YELLOW}{match_str}{RESET} {right}{suffix}"
        snippets.append(snip.strip())
        pos = idx + len(query) + 20
    return snippets


def format_text_results(
    matches: List[Tuple[str, str, int]],
    query: str,
    total_found: int,
    width: Optional[int] = None
) -> str:
    """Format full-text search results with snippet cards."""
    if width is None:
        term_width = shutil.get_terminal_size((80, 24)).columns
        width = max(60, min(100, term_width))

    inner_width = width - 4
    content_width = inner_width - 4

    lines = [""]
    for hw, eb, b in matches:
        lines.append(f"• {BOLD}{CYAN}{hw}{RESET}  {DIM}(block {b}){RESET}")
        snips = extract_snippets(eb, query, max_snippets=2)
        for s in snips:
            wrapped = wrap_ansi(f"  {s}", content_width)
            lines.extend(wrapped)
        lines.append("")

    title = f"FULL-TEXT SEARCH: \"{query}\" ({total_found:,} matching entr{'ies' if total_found != 1 else 'y'})"
    if len(matches) < total_found:
        title += f" [showing first {len(matches)}]"

    return render_box(title, lines, width=width, color_border=CYAN)


def cli_text_search(
    reader: OEDReader,
    query: str,
    limit: Optional[int] = None,
    full: bool = False,
    confirm_yes: bool = False,
    use_pager: bool = True
):
    query_clean = query.strip()
    if not query_clean:
        print("Please provide a text query to search for (e.g. ':text pin').")
        return

    is_tty = sys.stdout.isatty()
    def on_progress(cur, total):
        if is_tty:
            pct = (cur / total) * 100
            print(f"\r{CYAN}Searching full text for \"{query_clean}\" across {total:,} blocks... {pct:.0f}%{RESET}", end="", flush=True)

    matches = reader.search_text(query_clean, progress_callback=on_progress if is_tty else None)
    if is_tty:
        print("\r\033[K", end="", flush=True)

    total_matches = len(matches)
    if total_matches == 0:
        print(f"\nNo entries found containing \"{query_clean}\".\n")
        return

    # Warning & Confirmation if more than 50 entries
    display_list = matches
    if total_matches > 50 and not confirm_yes:
        print(f"\n{BOLD}{YELLOW}Warning:{RESET} Found {BOLD}{total_matches:,}{RESET} entries containing \"{query_clean}\".")
        try:
            resp = input(f"Display all {total_matches:,} entries? [y/N/limit] (or enter number like 50): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return

        if resp in ("y", "yes"):
            display_list = matches
        elif resp.isdigit() and int(resp) > 0:
            display_list = matches[:int(resp)]
        elif resp in ("50", "first 50"):
            display_list = matches[:50]
        else:
            print("Cancelled.")
            return
    elif limit and limit > 0:
        display_list = matches[:limit]

    if full:
        blocks = [OEDEntry(eb).format_terminal() for hw, eb, b in display_list]
        output = "\n\n" + ("\n" + "—" * 60 + "\n\n").join(blocks)
    else:
        output = "\n" + format_text_results(display_list, query_clean, total_found=total_matches)

    paginate(output, enabled=use_pager)


def cli_search(reader: OEDReader, prefix: str):
    results = reader.search_prefix(prefix, max_results=50)
    if not results:
        print(f"No headwords found matching prefix '{prefix}'.")
    else:
        output = "\n" + format_word_grid(results, title=f"MATCHES FOR PREFIX \"{prefix}\" ({len(results)} found)")
        print(output)


def cli_random(reader: OEDReader, use_pager: bool = True):
    entry = reader.random_entry()
    paginate(entry.format_terminal(), enabled=use_pager)


def cli_block(reader: OEDReader, block_idx: int, raw: bool = False, use_pager: bool = True):
    text = reader.decompress_block(block_idx)
    if raw:
        output = text
    else:
        hws = reader.get_block_headwords(block_idx)
        header = (
            f"\n{BOLD}Block {block_idx}:{RESET} {len(text):,} decompressed bytes\n"
            f"{CYAN}Headwords ({len(hws)}):{RESET} {', '.join(hws[:20])}{'...' if len(hws) > 20 else ''}\n\n"
        )
        output = header + text
    paginate(output, enabled=use_pager)


def cli_export(reader: OEDReader, output_file: str):
    print(f"Exporting {reader.total_blocks:,} blocks to {output_file}...")
    def on_progress(current, total):
        pct = (current / total) * 100
        print(f"\r  Progress: {current}/{total} blocks ({pct:.1f}%)", end="", flush=True)

    reader.export_sgml(output_file, callback=on_progress)
    print("\nExport completed successfully!")


def cli_man():
    """Display the oed(1) manual page."""
    man_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oed.1")
    if os.path.isfile(man_path):
        if sys.stdout.isatty():
            try:
                subprocess.run(["man", man_path], check=True)
                return
            except Exception:
                pass
        try:
            res = subprocess.run(["mandoc", man_path], stdout=subprocess.PIPE, text=True, check=True)
            paginate(res.stdout, enabled=True)
            return
        except Exception:
            pass
        with open(man_path, "r", encoding="utf-8") as f:
            paginate(f.read(), enabled=True)
    else:
        print("Manual page (oed.1) not found.")


def cli_shell(reader: OEDReader, use_pager: bool = True):
    print(f"{BOLD}Oxford English Dictionary 2nd Edition Interactive Shell{RESET}")
    print("Commands:  <word> [--all|--raw]  |  :end <suffix>  |  :text <string>  |  :search <prefix>  |  :man  |  :quit\n")
    while True:
        try:
            line = input(f"{CYAN}OED> {RESET}").strip()
            if not line:
                continue
            if line in (":quit", ":q", ":exit"):
                break
            elif line in (":man", "man", ":help", "help"):
                cli_man()
            elif line.startswith(":random"):
                cli_random(reader, use_pager=use_pager)
            elif line.startswith((":search ", "search ")):
                prefix_val = line[8:].strip() if line.startswith(":search ") else line[7:].strip()
                cli_search(reader, prefix_val)
            elif line == ":text" or line == "text":
                print("Usage: :text <query> [--full] [-n|--limit N] (e.g. ':text pin')")
            elif line.startswith((":text ", "text ")):
                sub_line = line[6:].strip() if line.startswith(":text ") else line[5:].strip()
                parts = sub_line.split()
                full_mode = False
                limit = None
                query_tokens = []
                idx = 0
                while idx < len(parts):
                    p = parts[idx]
                    if p in ("--full", "-f"):
                        full_mode = True
                    elif p in ("-n", "--limit") and idx + 1 < len(parts):
                        try:
                            limit = int(parts[idx+1])
                            idx += 1
                        except ValueError:
                            pass
                    else:
                        query_tokens.append(p)
                    idx += 1
                text_query = " ".join(query_tokens).strip()
                if not text_query:
                    print("Usage: :text <query> [--full] [-n|--limit N] (e.g. ':text pin')")
                    continue
                cli_text_search(reader, text_query, limit=limit, full=full_mode, use_pager=use_pager)
            elif line == ":end" or line == "end" or line == ":suffix" or line == ":ends-with":
                print("Usage: :end <suffix> [-s|--single] [-n|--limit N] [-l|--len] (e.g. ':end og')")
            elif line.startswith((":end ", ":suffix ", ":ends-with ", "end ")):
                sub_line = line
                for prefix in (":end", ":suffix", ":ends-with", "end"):
                    if sub_line.startswith(prefix + " "):
                        sub_line = sub_line[len(prefix) + 1:].strip()
                        break
                parts = sub_line.split()
                single_only = False
                sort_len = False
                limit = None
                suffix_parts = []
                idx = 0
                while idx < len(parts):
                    p = parts[idx]
                    if p in ("-s", "--single"):
                        single_only = True
                    elif p in ("--len", "--length", "-l"):
                        sort_len = True
                    elif p in ("-n", "--limit") and idx + 1 < len(parts):
                        try:
                            limit = int(parts[idx+1])
                            idx += 1
                        except ValueError:
                            pass
                    else:
                        suffix_parts.append(p)
                    idx += 1
                suffix_val = " ".join(suffix_parts).strip()
                if not suffix_val:
                    print("Usage: :end <suffix> [-s|--single] [-n|--limit N] (e.g. ':end og')")
                    continue
                cli_end_search(reader, suffix_val, single_only=single_only, sort_len=sort_len, limit=limit, use_pager=use_pager)
            elif line.startswith(":block "):
                try:
                    b_num = int(line[7:].strip())
                    cli_block(reader, b_num, use_pager=use_pager)
                except ValueError:
                    print("Invalid block number.")
            else:
                tokens = line.split()
                raw_mode = False
                all_mode = False
                word_tokens = []

                for token in tokens:
                    if token in ("--raw", "-r", ":raw"):
                        raw_mode = True
                    elif token in ("--all", "-a", ":all"):
                        all_mode = True
                    elif token.lower() in ("lookup", ":lookup", "find", ":find"):
                        continue
                    else:
                        word_tokens.append(token)

                word_to_lookup = " ".join(word_tokens).strip()
                if not word_to_lookup:
                    print("Please specify a word to look up.")
                    continue

                cli_lookup(reader, word_to_lookup, raw=raw_mode, all_quotes=all_mode, use_pager=use_pager)
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break


def main():
    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument(
        "--no-pager", action="store_true",
        help="Disable pausing/paging of output"
    )

    parser = argparse.ArgumentParser(
        description="Access and query the Oxford English Dictionary 2nd Edition CD-ROM (OED2.DAT).",
        parents=[common_parent]
    )
    parser.add_argument(
        "-f", "--dat-file", default=None,
        help="Path to OED2.DAT file (auto-detected if omitted)"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # lookup
    p_lookup = subparsers.add_parser("lookup", help="Look up a word in the dictionary", parents=[common_parent])
    p_lookup.add_argument("word", help="The word to look up")
    p_lookup.add_argument("--all", "-a", action="store_true", help="Display all historical quotations")
    p_lookup.add_argument("--raw", action="store_true", help="Print raw SGML instead of formatted output")

    # search
    p_search = subparsers.add_parser("search", help="Search headwords by prefix", parents=[common_parent])
    p_search.add_argument("prefix", help="Prefix to search")

    # end (suffix search)
    p_end = subparsers.add_parser(
        "end",
        aliases=["suffix", "ends-with"],
        help="Search headwords by suffix / ending letters (e.g. 'end og')",
        parents=[common_parent]
    )
    p_end.add_argument("suffix", help="Ending letters / suffix to search for (e.g. 'og')")
    p_end.add_argument("-s", "--single", action="store_true", help="Only show single words (exclude hyphens and multi-word terms)")
    p_end.add_argument("-n", "--limit", type=int, default=None, help="Limit number of results displayed")
    p_end.add_argument("-l", "--len", action="store_true", help="Sort results by length first")

    # text (full-text search)
    p_text = subparsers.add_parser(
        "text",
        aliases=["fulltext", "find-text"],
        help="Search for a (sub)string within the entire text of the dictionary",
        parents=[common_parent]
    )
    p_text.add_argument("query", help="Text or substring to search for (e.g. 'pin')")
    p_text.add_argument("-n", "--limit", type=int, default=None, help="Limit number of results displayed")
    p_text.add_argument("--full", action="store_true", help="Display full formatted entries instead of snippets")
    p_text.add_argument("-y", "--yes", action="store_true", help="Proceed without confirmation prompt if > 50 entries")

    # random
    subparsers.add_parser("random", help="Display a random dictionary entry", parents=[common_parent])

    # info
    subparsers.add_parser("info", help="Show volume and data file information", parents=[common_parent])

    # block
    p_block = subparsers.add_parser("block", help="Inspect a specific 32KB block", parents=[common_parent])
    p_block.add_argument("number", type=int, help="Block number (0 - 7828)")
    p_block.add_argument("--raw", action="store_true", help="Print raw decompressed SGML")

    # export
    p_export = subparsers.add_parser("export", help="Export full dictionary to an SGML text file", parents=[common_parent])
    p_export.add_argument("output", help="Output file path (e.g. OED2.sgml)")

    # shell
    subparsers.add_parser("shell", help="Launch interactive lookup shell", parents=[common_parent])

    # man
    subparsers.add_parser("man", help="Display the complete manual page with all options", parents=[common_parent])

    args = parser.parse_args()

    if args.command == "man":
        cli_man()
        return

    try:
        reader = OEDReader(args.dat_file)
    except FileNotFoundError as e:
        print(f"{BOLD}\033[31mError:{RESET} {e}", file=sys.stderr)
        sys.exit(1)

    use_pager = not args.no_pager

    with reader:
        if args.command == "lookup":
            cli_lookup(reader, args.word, raw=args.raw, all_quotes=args.all, use_pager=use_pager)
        elif args.command == "search":
            cli_search(reader, args.prefix)
        elif args.command in ("end", "suffix", "ends-with"):
            cli_end_search(reader, args.suffix, single_only=args.single, sort_len=args.len, limit=args.limit, use_pager=use_pager)
        elif args.command in ("text", "fulltext", "find-text"):
            cli_text_search(reader, args.query, limit=args.limit, full=args.full, confirm_yes=args.yes, use_pager=use_pager)
        elif args.command == "random":
            cli_random(reader, use_pager=use_pager)
        elif args.command == "info":
            cli_info(reader)
        elif args.command == "block":
            cli_block(reader, args.number, raw=args.raw, use_pager=use_pager)
        elif args.command == "export":
            cli_export(reader, args.output)
        elif args.command == "shell":
            cli_shell(reader, use_pager=use_pager)
        else:
            # If no command provided, show info and launch shell
            cli_info(reader)
            cli_shell(reader, use_pager=use_pager)


if __name__ == "__main__":
    main()
