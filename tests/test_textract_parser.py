from idp.adapters.textract import parse_blocks, table_to_text


def _word(i, text, page=1, conf=99.0):
    return {"Id": f"w{i}", "BlockType": "WORD", "Text": text, "Page": page, "Confidence": conf}


def test_parses_lines_key_values_and_tables():
    blocks = [
        _word(1, "Invoice"), _word(2, "Number:"), _word(3, "INV-1"),
        {"Id": "l1", "BlockType": "LINE", "Text": "Invoice Number: INV-1", "Page": 1, "Confidence": 97.0},
        {"Id": "k1", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["KEY"], "Page": 1, "Confidence": 90.0,
         "Relationships": [{"Type": "CHILD", "Ids": ["w1", "w2"]}, {"Type": "VALUE", "Ids": ["v1"]}]},
        {"Id": "v1", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["VALUE"], "Page": 1, "Confidence": 85.0,
         "Relationships": [{"Type": "CHILD", "Ids": ["w3"]}]},
        _word(4, "Qty", page=2), _word(5, "2", page=2),
        {"Id": "c1", "BlockType": "CELL", "RowIndex": 1, "ColumnIndex": 1, "Page": 2, "Confidence": 80.0,
         "Relationships": [{"Type": "CHILD", "Ids": ["w4"]}]},
        {"Id": "c2", "BlockType": "CELL", "RowIndex": 2, "ColumnIndex": 1, "Page": 2, "Confidence": 70.0,
         "Relationships": [{"Type": "CHILD", "Ids": ["w5"]}]},
        {"Id": "t1", "BlockType": "TABLE", "Page": 2, "Relationships": [{"Type": "CHILD", "Ids": ["c1", "c2"]}]},
    ]
    doc = parse_blocks("d1", blocks)
    assert doc.pages == 2
    assert doc.lines[0].text == "Invoice Number: INV-1"
    kv = doc.key_values[0]
    assert (kv.key, kv.value) == ("Invoice Number", "INV-1")
    assert kv.confidence == 85.0  # weakest link of key and value
    assert doc.tables[0].rows == [["Qty"], ["2"]]
    assert doc.tables[0].confidence == 70.0
    assert table_to_text(doc.tables[0]) == "Qty\n2"


def test_low_confidence_pages_are_reported():
    blocks = [
        {"Id": "a", "BlockType": "LINE", "Text": "clear", "Page": 1, "Confidence": 99.0},
        {"Id": "b", "BlockType": "LINE", "Text": "smudged", "Page": 2, "Confidence": 41.0},
    ]
    doc = parse_blocks("d", blocks)
    assert doc.low_confidence_pages == [2]
    assert doc.mean_ocr_confidence == 70.0
