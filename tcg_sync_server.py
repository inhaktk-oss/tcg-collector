#!/usr/bin/env python3
"""
TCG Collection Sync Server
- REST API for card metadata sync (SQLite)
- Photo storage (file system)
- Static file serving (TCG app)
- Bearer token auth

Env vars:
  TCG_SYNC_TOKEN  — required, bearer auth token
  TCG_DATA_DIR    — data directory (default: ~/tcg-data)
  TCG_PORT        — port (default: 8082)
"""
import os, json, time, sqlite3, hashlib, base64, hmac, re
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

# ─── Config ───
DATA_DIR = Path(os.environ.get('TCG_DATA_DIR', os.path.expanduser('~/tcg-data')))
PHOTOS_DIR = DATA_DIR / 'photos'
DB_PATH = DATA_DIR / 'tcg.db'
SYNC_TOKEN = os.environ.get('TCG_SYNC_TOKEN', '')
PORT = int(os.environ.get('TCG_PORT', 8082))
STATIC_DIR = Path(__file__).parent  # Serve index.html etc from same dir

DATA_DIR.mkdir(parents=True, exist_ok=True)
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_ORIGINS = [
    'https://inhaktk-oss.github.io',
    'http://localhost:8082',
    'http://localhost:3000',
]

# ─── Database ───
_db_lock = threading.Lock()

def get_db():
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=5000')
    return db

def init_db():
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS cards (
        id TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        updated_at INTEGER NOT NULL
    )''')
    db.execute('''CREATE INDEX IF NOT EXISTS idx_cards_updated ON cards(updated_at)''')
    db.commit()
    db.close()

init_db()

# ─── Helpers ───
def safe_card_id(card_id):
    """Sanitize card_id to prevent path traversal."""
    return re.sub(r'[^a-zA-Z0-9_-]', '', card_id)

def save_photo_file(card_id, data_url):
    """Save base64 data URL as JPEG file."""
    try:
        cid = safe_card_id(card_id)
        if not cid:
            return
        if ',' in data_url:
            b64 = data_url.split(',', 1)[1]
        else:
            b64 = data_url
        img_bytes = base64.b64decode(b64)
        if len(img_bytes) > 5 * 1024 * 1024:  # 5MB max
            return
        (PHOTOS_DIR / f'{cid}.jpg').write_bytes(img_bytes)
    except Exception as e:
        print(f'[Photo] Save error for {card_id}: {e}')

def check_auth(headers):
    """Verify bearer token."""
    if not SYNC_TOKEN:
        return False
    auth = headers.get('Authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else ''
    if not token:
        return False
    return hmac.compare_digest(token, SYNC_TOKEN)

# ─── HTTP Handler ───
class SyncHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format, *args):
        # Compact logging
        print(f'[{self.log_date_time_string()}] {args[0]}')

    def _cors_headers(self):
        origin = self.headers.get('Origin', '')
        if origin in ALLOWED_ORIGINS or origin.startswith('http://168.') or origin.startswith('http://localhost'):
            self.send_header('Access-Control-Allow-Origin', origin)
        else:
            self.send_header('Access-Control-Allow-Origin', ALLOWED_ORIGINS[0])
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Max-Age', '86400')

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, msg):
        self._json_response({'error': msg}, status)

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 50 * 1024 * 1024:  # 50MB max
            return None
        return self.rfile.read(length)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        # API routes
        if path == '/api/health':
            return self._handle_health()
        if path == '/api/cards':
            return self._handle_get_cards()
        if path.startswith('/api/photos/list'):
            return self._handle_list_photos()
        if path.startswith('/api/photos/'):
            return self._handle_get_photo(path)

        # Static files — serve TCG app
        if path == '/' or path == '':
            self.path = '/index.html'
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == '/api/cards/sync':
            return self._handle_sync()
        if path == '/api/photos/batch':
            return self._handle_batch_photos()
        if path.startswith('/api/photos/'):
            return self._handle_upload_photo(path)

        self._error(404, 'Not found')

    # ─── API Handlers ───

    def _handle_health(self):
        db = get_db()
        count = db.execute('SELECT COUNT(*) FROM cards').fetchone()[0]
        db.close()
        photos = len(list(PHOTOS_DIR.glob('*.jpg')))
        self._json_response({'status': 'ok', 'cards': count, 'photos': photos})

    def _handle_get_cards(self):
        if not check_auth(self.headers):
            return self._error(401, 'Unauthorized')
        db = get_db()
        rows = db.execute('SELECT id, data FROM cards ORDER BY updated_at DESC').fetchall()
        db.close()
        cards = []
        for r in rows:
            card = json.loads(r['data'])
            cid = safe_card_id(card.get('id', ''))
            photo_path = PHOTOS_DIR / f'{cid}.jpg'
            card['hasServerPhoto'] = photo_path.exists()
            card.pop('syncPhoto', None)
            card.pop('photo', None)
            cards.append(card)
        self._json_response({'cards': cards, 'lastSync': int(time.time() * 1000)})

    def _handle_sync(self):
        if not check_auth(self.headers):
            return self._error(401, 'Unauthorized')
        raw = self._read_body()
        if raw is None:
            return self._error(413, 'Payload too large')
        try:
            data = json.loads(raw)
        except:
            return self._error(400, 'Invalid JSON')

        client_cards = data.get('cards', [])
        deleted_ids = set(data.get('deletedIds', []))

        with _db_lock:
            db = get_db()
            # Get server cards
            rows = db.execute('SELECT id, data FROM cards').fetchall()
            server_map = {}
            for r in rows:
                server_map[r['id']] = json.loads(r['data'])

            merged = {}

            # Start with server cards
            for cid, card in server_map.items():
                if cid not in deleted_ids:
                    merged[cid] = card

            # Merge client cards
            for card in client_cards:
                card_id = card.get('id', '')
                if not card_id or card_id in deleted_ids:
                    continue

                # Extract photo if present
                sync_photo = card.pop('syncPhoto', None) or card.pop('photo', None)
                if sync_photo and isinstance(sync_photo, str) and sync_photo.startswith('data:'):
                    save_photo_file(card_id, sync_photo)

                existing = merged.get(card_id)
                if not existing:
                    merged[card_id] = card
                else:
                    client_time = max(card.get('updatedAt', 0), card.get('createdAt', 0))
                    server_time = max(existing.get('updatedAt', 0), existing.get('createdAt', 0))
                    if client_time >= server_time:
                        merged[card_id] = card

            # Process deletes
            for did in deleted_ids:
                merged.pop(did, None)
                db.execute('DELETE FROM cards WHERE id = ?', (did,))
                photo_path = PHOTOS_DIR / f'{safe_card_id(did)}.jpg'
                if photo_path.exists():
                    photo_path.unlink()

            # Save all merged cards to DB
            now = int(time.time() * 1000)
            for card_id, card in merged.items():
                card_json = json.dumps(card, ensure_ascii=False)
                updated = max(card.get('updatedAt', 0), card.get('createdAt', now))
                db.execute(
                    'INSERT INTO cards (id, data, updated_at) VALUES (?, ?, ?) '
                    'ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at',
                    (card_id, card_json, updated)
                )
            db.commit()
            db.close()

        # Return merged cards with photo info
        result_cards = list(merged.values())
        for card in result_cards:
            cid = safe_card_id(card.get('id', ''))
            card['hasServerPhoto'] = (PHOTOS_DIR / f'{cid}.jpg').exists()
        result_cards.sort(key=lambda c: c.get('createdAt', 0), reverse=True)

        self._json_response({
            'cards': result_cards,
            'lastSync': now,
            'count': len(result_cards)
        })

    def _handle_get_photo(self, path):
        if not check_auth(self.headers):
            return self._error(401, 'Unauthorized')
        card_id = safe_card_id(path.split('/api/photos/')[-1])
        photo_path = PHOTOS_DIR / f'{card_id}.jpg'
        if not photo_path.exists():
            return self._error(404, 'Photo not found')

        img = photo_path.read_bytes()
        self.send_response(200)
        self._cors_headers()
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(img)))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.end_headers()
        self.wfile.write(img)

    def _handle_upload_photo(self, path):
        if not check_auth(self.headers):
            return self._error(401, 'Unauthorized')
        card_id = safe_card_id(path.split('/api/photos/')[-1])
        if not card_id:
            return self._error(400, 'Invalid card ID')

        raw = self._read_body()
        if raw is None:
            return self._error(413, 'Payload too large')

        ct = self.headers.get('Content-Type', '')
        if 'json' in ct:
            data = json.loads(raw)
            photo = data.get('photo', '')
            if photo:
                save_photo_file(card_id, photo)
                self._json_response({'ok': True})
            else:
                self._error(400, 'No photo data')
        else:
            # Raw binary
            if len(raw) > 5 * 1024 * 1024:
                return self._error(413, 'Photo too large')
            (PHOTOS_DIR / f'{card_id}.jpg').write_bytes(raw)
            self._json_response({'ok': True})

    def _handle_batch_photos(self):
        if not check_auth(self.headers):
            return self._error(401, 'Unauthorized')
        raw = self._read_body()
        if raw is None:
            return self._error(413, 'Payload too large')
        data = json.loads(raw)
        photos = data.get('photos', {})
        saved = 0
        for card_id, photo_data in photos.items():
            cid = safe_card_id(card_id)
            if cid and photo_data and isinstance(photo_data, str) and photo_data.startswith('data:'):
                save_photo_file(cid, photo_data)
                saved += 1
        self._json_response({'ok': True, 'saved': saved})

    def _handle_list_photos(self):
        if not check_auth(self.headers):
            return self._error(401, 'Unauthorized')
        ids = [p.stem for p in PHOTOS_DIR.glob('*.jpg')]
        self._json_response({'photoIds': ids})


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


if __name__ == '__main__':
    if not SYNC_TOKEN:
        print('⚠️  TCG_SYNC_TOKEN 환경변수를 설정하세요!')
        print('   예: export TCG_SYNC_TOKEN="my-secret-token-123"')
        exit(1)

    print(f'🃏 TCG Sync Server starting on port {PORT}')
    print(f'   Data dir: {DATA_DIR}')
    print(f'   Photos:   {PHOTOS_DIR}')
    print(f'   Static:   {STATIC_DIR}')

    server = ReusableHTTPServer(('0.0.0.0', PORT), SyncHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n🛑 Server stopped')
        server.server_close()
