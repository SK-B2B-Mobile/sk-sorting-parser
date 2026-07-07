"""
SK B2B Fulfillment — PDF 파싱 API 서버 (클라우드 배포용)
================================================================
GitHub Pages에 올린 화면이 이 서버로 PDF를 보내면,
검증된 pdfplumber 로직으로 정확하게 파싱해서 JSON으로 돌려줍니다.

[ 배포 방법 ]
  Render.com (무료 티어) 기준:
    1. 이 폴더(server 폴더)를 GitHub 저장소로 push
    2. Render에서 New > Web Service > 저장소 연결
    3. Build Command:  pip install -r requirements.txt
    4. Start Command:  gunicorn app:app
    5. 배포 완료되면 https://your-app.onrender.com 주소가 나옴
       → 이 주소를 화면(HTML)의 API_BASE 에 넣으면 끝

[ 로컬 테스트 ]
    pip install flask flask-cors pdfplumber gunicorn
    python app.py
    → http://localhost:5000 에서 동작
"""
import io
import re
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber

app = Flask(__name__)
# GitHub Pages 등 외부 도메인에서 호출 허용
CORS(app)

# ════════════════════════════════════════════════════════════
# PDF 파서 (검증된 로직)
#  - 줄바꿈으로 쪼개진 SKU 복원
#  - Request Q'ty(row[4]) 사용 — Invc Q'ty 아님
# ════════════════════════════════════════════════════════════
BARCODE_RE = re.compile(r'\b(\d{12,14}|X\w{9,10})\b')
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
    """SKU 셀 안의 줄바꿈/공백 전부 제거 (긴 SKU가 두 줄로 쪼개진 경우 복원)."""
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


def parse_pdf_bytes(pdf_bytes):
    items = []
    all_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ''
            all_text.append(page_text)
            for table in page.extract_tables():
                for row in table:
                    if not is_data_row(row):
                        continue
                    if len(row) < 7:
                        continue
                    sku = clean_sku(row[0])
                    name = clean_text(row[1])
                    barcode = extract_barcode(row[2])
                    req_qty = parse_int(row[4])   # Request Q'ty (실제 수량)
                    rack = extract_rack(row[5])
                    if not barcode or not rack:
                        continue
                    items.append({
                        'sku': sku, 'name': name, 'barcode': barcode,
                        'req_qty': req_qty, 'rack': rack,
                    })
    meta = parse_metadata('\n'.join(all_text))
    return {'items': items, 'meta': meta}


# ════════════════════════════════════════════════════════════
# API 엔드포인트
# ════════════════════════════════════════════════════════════
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
