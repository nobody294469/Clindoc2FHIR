"""Tests for Native Tesseract TSV parsing, confidence normalization, and token preservation."""
import pytest
from clindoc.ocr.preprocessing import AffineMatrix, OrientationResult, parse_osd_output
from clindoc.ocr.tesseract_runner import (
    parse_tesseract_tsv,
    check_tesseract_readiness,
    find_tesseract_binary,
)


SAMPLE_TSV = """level	page_num	block_num	par_num	line_num	word_num	left	top	width	height	conf	text
1	1	0	0	0	0	0	0	1000	1000	-1	
2	1	1	0	0	0	50	50	400	50	-1	
3	1	1	1	0	0	50	50	400	50	-1	
4	1	1	1	1	0	50	50	400	20	-1	
5	1	1	1	1	1	50	50	60	20	95.5	Lisinopril
5	1	1	1	1	2	120	50	30	20	88.2	10
5	1	1	1	1	3	160	50	30	20	91.0	mg
4	1	1	1	2	0	50	80	400	20	-1	
5	1	1	1	2	1	50	80	70	20	12.4	fuzzy_word
5	1	1	1	2	2	130	80	60	20	0.0	very_low
"""


def test_parse_tesseract_tsv_preserves_low_confidence():
    """Verify that low-confidence tokens (down to 0.0) are completely preserved (zero deletion)."""
    matrix = AffineMatrix.identity()
    words, lines = parse_tesseract_tsv(
        tsv_text=SAMPLE_TSV,
        forward_matrix=matrix,
        page_num=1,
        canonical_width=1000.0,
        canonical_height=1000.0,
    )

    assert len(words) == 5
    assert [w.text for w in words] == ["Lisinopril", "10", "mg", "fuzzy_word", "very_low"]

    # Verify confidence normalization [0.0, 1.0]
    assert words[0].confidence == 0.955
    assert words[1].confidence == 0.882
    assert words[2].confidence == 0.91
    assert words[3].confidence == 0.124
    assert words[4].confidence == 0.0  # Zero confidence preserved!

    # Verify lines
    assert len(lines) == 2
    assert lines[0].text == "Lisinopril 10 mg"
    assert lines[1].text == "fuzzy_word very_low"


def test_check_tesseract_readiness():
    is_ready, msg, details = check_tesseract_readiness()
    assert is_ready is True
    assert "tesseract" in msg.lower()
    assert details.get("has_eng_traineddata") is True
    assert details.get("has_osd_traineddata") is True


def test_parse_osd_output():
    raw_osd = """Page number: 0
Orientation in degrees: 270
Rotate: 90
Orientation confidence: 15.80
Script: Latin
Script confidence: 9.20
"""
    res = parse_osd_output(raw_osd, min_confidence=1.5)
    assert res.orientation_degrees == 270
    assert res.rotate_cw_degrees == 90
    assert res.confidence == 15.80
    assert res.script == "Latin"


def test_parse_osd_output_low_confidence_fallback():
    raw_osd = """Page number: 0
Orientation in degrees: 180
Rotate: 180
Orientation confidence: 0.40
Script: Latin
Script confidence: 1.20
"""
    res = parse_osd_output(raw_osd, min_confidence=1.5)
    # Below threshold: fallback to 0 rotation
    assert res.rotate_cw_degrees == 0

