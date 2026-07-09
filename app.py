"""
SK B2B Fulfillment — PDF 파싱 API 서버 (클라우드 배포용)
"""
import io
import re
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber

app = Flask(__name__)
CORS(app)

BARCODE_RE = re.compile(r'\b(\d{2,4}[A-Z]{1,3}\d{6,12}|\d{12,14}|X\w{9,10})\b')
# ↑ 2026-07-09 수정: 순수 12~14자리 숫자 / ASIN(X+영숫자) 외에,
#   "880SG00002045" 처럼 숫자 사이에 2~3자리 영문(국가/타입 코드)이 낀
#   샘플·비매품(NOT FOR SALE) 상품용 특수 바코드 포맷도 인식하도록 확장.
#   이 포맷을 못 읽으면 해당 줄이 flush 안 되고 다음 상품 줄과 통째로
#   합쳐지는(SKU/수량 뒤섞임) 심각한 버그로 이어짐 — B0709-AM 배치에서 발견.
RACK_RE = re.compile(r'\b([A-Z]{2,3}-[A-Z]-\d{1,2}(?:-\d{1,2})?)\b')
INTEGER_RE = re.compile(r'^\d[\d,]*$')
INVOICE_RE = re.compile(r'\b(I[NM]\d{8}|CG\d{8,9})\b')
SHIP_DATE_LABEL = re.compile(r'Shipping\s*Date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})', re.I)
SHIP_VIA_RE = re.compile(r'Ship\s*Via\s*[:\-]?\s*(\S+)', re.I)


def extract_barcode(cell):
    if not cell:
        return None
    m = BARCODE_RE.search(cell)
    return m.group(1) if m else None


def extract_rack(cell):
    if not cell:
        return None
    m = RACK_RE.search(cell)
    return m.group(1) if m else None


def clean_text(cell):
    if not cell:
        return ''
    return ' '.join(cell.split())


def clean_sku(cell):
    if not cell:
        return ''
    return re.sub(r'\s+', '', cell.strip())


def parse_int(cell):
    if not cell:
        return 0
    m = INTEGER_RE.match(cell.strip())
    return int(m.group(0).replace(',', '')) if m else 0


def is_data_row(row):
    if not row or not row[0]:
        return False
    first = clean_sku(row[0])
    if first in ('SKU', 'TOTAL', ''):
        return False
    if not re.match(r'^[A-Za-z0-9_\-.]+$', first):
        return False
    return True


def parse_metadata(text):
    meta = {'invoice_no': None, 'customer': None, 'ship_date': None, 'ship_via': None}

    inv = INVOICE_RE.search(text)
    if inv:
        meta['invoice_no'] = inv.group(1)

    sd = SHIP_DATE_LABEL.search(text)
    if sd:
        meta['ship_date'] = sd.group(1)

    via = SHIP_VIA_RE.search(text)
    if via:
        v = via.group(1).upper()
        if v in ('UPS', 'FEDEX', 'DHL'):
            meta['ship_via'] = 'UPS'
        elif v in ('PU', 'PICKUP', 'PICK-UP'):
            meta['ship_via'] = 'PU'
        elif v in ('TK', 'TRUCK'):
            meta['ship_via'] = 'TK'
        else:
            meta['ship_via'] = v
    if not meta['ship_via'] and re.search(r'Customer\s*Pick\s*Up', text, re.I):
        meta['ship_via'] = 'PU'
    if not meta['ship_via']:
        meta['ship_via'] = 'UPS'

    EXCLUDE = re.compile(
        r'^(Shipment|Ship Via|Registrant|Req Date|Shipping Date|Double check|'
        r'Customer Pick Up|#\d Picking|Box QTY|BP wholesale|FIRST ORDER|SKU|'
        r'TOTAL|bar code|Q\'ty|Product name|Invc|Request|Stock)', re.I)
    for ln in [l.strip() for l in text.split('\n') if l.strip()]:
        if len(ln) > 40 or len(ln) < 3:
            continue
        if EXCLUDE.search(ln):
            continue
        if INVOICE_RE.match(ln):
            continue
        if re.search(r'wholesale$', ln, re.I):
            continue
        ascii_ratio = sum(1 for c in ln if ord(c) < 128) / len(ln)
        if ascii_ratio < 0.85:
            continue
        if re.match(r'^[\d\s\-.,()]+$', ln):
            continue
        if not re.match(r'^[A-Za-z]', ln):
            continue
        meta['customer'] = ln
        break

    return meta


def _merge_fine_rows_to_items(fine_rows):
    HEADER_MARKERS = {
        'SKU', 'Product name', 'bar code', 'Invc', "Q'ty",
        'Request', 'Rack code', "Stock Q'ty", "(pick Q'ty)"
    }
    buf = {k: [] for k in ('sku', 'name', 'c2', 'c4', 'c5')}
    results = []

    def flush():
        nonlocal buf
        barcode = next((m.group(1) for c in buf['c2']
                         if (m := BARCODE_RE.search(c))), None)
        rack = next((m.group(1) for c in buf['c5']
                      if (m := RACK_RE.search(c))), None)
        req_qty = next((int(c.strip().replace(',', '')) for c in buf['c4']
                         if INTEGER_RE.match(c.strip())), None)
        sku = clean_sku(''.join(buf['sku']))
        name = clean_text(' '.join(buf['name']))
        if barcode and rack and req_qty is not None and sku:
            results.append({'sku': sku, 'name': name, 'barcode': barcode,
                             'req_qty': req_qty, 'rack': rack})
        buf = {k: [] for k in ('sku', 'name', 'c2', 'c4', 'c5')}

    for row in fine_rows:
        cells = [(c or '').strip() for c in row]
        if len(cells) < 7:
            continue
        if any(c in HEADER_MARKERS for c in cells):
            continue
        if not any(cells):
            continue
        if cells[0]: buf['sku'].append(cells[0])
        if cells[1]: buf['name'].append(cells[1])
        if cells[2]: buf['c2'].append(cells[2])
        if cells[4]: buf['c4'].append(cells[4])
        if cells[5]: buf['c5'].append(cells[5])
        if cells[2] and BARCODE_RE.search(cells[2]):
            flush()
    if any(buf.values()):
        flush()
    return results


def parse_pdf_bytes(pdf_bytes):
    items = []
    all_text = []
    seen = set()
    table_settings = {"vertical_strategy": "lines", "horizontal_strategy": "text"}

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ''
            all_text.append(page_text)

            fine_tables = page.extract_tables(table_settings=table_settings)
            found_any = False
            for table in fine_tables:
                for item in _merge_fine_rows_to_items(table):
                    found_any = True
                    key = (item['barcode'], item['rack'], item['req_qty'], item['sku'])
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(item)

            if not found_any:
                for table in page.extract_tables():
                    for row in table:
                        if not is_data_row(row) or len(row) < 7:
                            continue
                        sku = clean_sku(row[0])
                        name = clean_text(row[1])
                        barcode = extract_barcode(row[2])
                        req_qty = parse_int(row[4])
                        rack = extract_rack(row[5])
                        if not barcode or not rack:
                            continue
                        key = (barcode, rack, req_qty, sku)
                        if key in seen:
                            continue
                        seen.add(key)
                        items.append({
                            'sku': sku, 'name': name, 'barcode': barcode,
                            'req_qty': req_qty, 'rack': rack,
                    })
    meta = parse_metadata('\n'.join(all_text))
    return {'items': items, 'meta': meta}


@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'SK Fulfillment PDF Parser', 'version': '1.0'})


@app.route('/api/parse', methods=['POST'])
def parse():
    try:
        if 'pdf' not in request.files:
            return jsonify({'error': 'PDF 파일이 없습니다'}), 400
        f = request.files['pdf']
        pdf_bytes = f.read()
        result = parse_pdf_bytes(pdf_bytes)
        result['filename'] = f.filename
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
