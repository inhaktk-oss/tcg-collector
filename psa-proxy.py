#!/usr/bin/env python3
"""
PSA 로컬 프록시 서버 — Playwright로 Cloudflare 우회
사용법: python3 psa-proxy.py
       → http://localhost:8878 에서 실행
웹앱이 자동으로 이 서버에 요청을 보냅니다.
"""

import asyncio
import json
import re
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from threading import Thread

# ─── Config ───
PORT = 8878
BROWSER_HEADLESS = True  # True: 백그라운드, False: 브라우저 창 표시

_browser = None
_playwright = None
_lock = asyncio.Lock()
_loop = None


async def init_browser():
    global _browser, _playwright
    if _browser:
        return _browser
    from playwright.async_api import async_playwright
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=False,
        args=['--headless=new', '--disable-blink-features=AutomationControlled']
    )
    return _browser


async def fetch_psa_cert(cert_number):
    """Playwright로 PSA cert 페이지에서 정보 + 이미지 추출"""
    browser = await init_browser()
    context = await browser.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    page = await context.new_page()

    try:
        url = f'https://www.psacard.com/cert/{cert_number}'
        await page.goto(url, wait_until='domcontentloaded', timeout=30000)

        # Cloudflare 챌린지 대기 (최대 20초)
        for _ in range(40):
            cf = await page.evaluate('document.body.innerText.includes("Just a moment")')
            if not cf:
                break
            await asyncio.sleep(0.5)
        else:
            return {'error': 'cloudflare', 'message': 'Cloudflare challenge timeout'}

        # 페이지 완전 로드 대기
        await asyncio.sleep(2)

        # 데이터 추출
        info = await page.evaluate('''() => {
            const info = {};
            
            // Method 1: dt/dd pairs (PSA uses definition lists)
            const kv = {};
            document.querySelectorAll('dt').forEach(dt => {
                const dd = dt.nextElementSibling;
                if (dd) kv[dt.textContent.trim()] = dd.textContent.trim();
            });
            // Also try th/td
            document.querySelectorAll('th').forEach(th => {
                const td = th.nextElementSibling;
                if (td && td.tagName === 'TD' && !kv[th.textContent.trim()]) {
                    kv[th.textContent.trim()] = td.textContent.trim();
                }
            });
            
            if (kv['Year']) info.year = kv['Year'];
            if (kv['Brand/Title']) info.brand = kv['Brand/Title'];
            if (kv['Subject']) info.subject = kv['Subject'];
            if (kv['Card Number']) info.cardNumber = kv['Card Number'];
            if (kv['Category']) info.category = kv['Category'];
            if (kv['Item Grade']) info.gradeLabel = kv['Item Grade'];
            
            // Build name from parts
            const parts = [info.year, info.brand, '#' + (info.cardNumber || ''), info.subject].filter(v => v && v !== '#');
            info.name = parts.join(' ');
            
            // Estimate, Population from header section (bold text on page)
            const text = document.body.innerText;
            const estM = text.match(/(?:PSA )?ESTIMATE\s*\$?([\d,.]+)/i); 
            if (estM) info.estimate = estM[1].replace(/,/g, '');
            const popM = text.match(/(?:PSA )?POPULATION\s*([\d,.]+)/i);
            if (popM) info.pop = popM[1].replace(/,/g, '');
            const phM = text.match(/(?:PSA )?POP HIGHER\s*([\d,.]+)/i);
            if (phM) info.popHigher = phM[1].replace(/,/g, '');
            
            // 이미지 URL 추출 (CloudFront CDN)
            const certImgs = Array.from(document.querySelectorAll('img'))
                .map(img => img.src)
                .filter(src => src.includes('d1htnxwo4o0jhw.cloudfront.net/cert/'));
            if (certImgs.length > 0) info.imageUrl = certImgs[0].replace('/small/', '/');
            if (certImgs.length > 1) info.imageUrl2 = certImgs[1].replace('/small/', '/');
            
            info._source = 'psa-local-proxy';
            return info;
        }''')

        # 이미지를 프록시 브라우저에서 직접 캡처 (CDN CORS 실패 대비)
        try:
            img_data = await page.evaluate('''async () => {
                const imgs = Array.from(document.querySelectorAll('img'))
                    .filter(i => i.src.includes('d1htnxwo4o0jhw.cloudfront.net/cert/') && i.naturalWidth > 50);
                if (imgs.length === 0) return null;
                const results = {};
                for (let idx = 0; idx < Math.min(imgs.length, 2); idx++) {
                    const img = imgs[idx];
                    // Full-size URL
                    const fullUrl = img.src.replace('/small/', '/');
                    try {
                        const resp = await fetch(fullUrl);
                        const blob = await resp.blob();
                        const reader = new FileReader();
                        const dataUrl = await new Promise((resolve) => {
                            reader.onload = () => resolve(reader.result);
                            reader.readAsDataURL(blob);
                        });
                        results[idx === 0 ? 'front' : 'back'] = dataUrl;
                    } catch(e) {
                        // Fallback: canvas capture from rendered img
                        try {
                            const c = document.createElement('canvas');
                            c.width = img.naturalWidth; c.height = img.naturalHeight;
                            c.getContext('2d').drawImage(img, 0, 0);
                            results[idx === 0 ? 'front' : 'back'] = c.toDataURL('image/jpeg', 0.9);
                        } catch(e2) {}
                    }
                }
                return Object.keys(results).length > 0 ? results : null;
            }''')
            if img_data:
                if img_data.get('front'):
                    info['imageBase64'] = img_data['front']
                if img_data.get('back'):
                    info['imageBase64_back'] = img_data['back']
        except Exception as e:
            print(f'  ⚠️ 이미지 캡처 실패: {e}')

        return info

    except Exception as e:
        return {'error': str(e)}
    finally:
        await context.close()


async def fetch_any_url(target_url):
    """범용 CORS 프록시 — 임의 URL의 HTML 가져오기"""
    browser = await init_browser()
    context = await browser.new_context(
        user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    )
    page = await context.new_page()
    try:
        await page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
        # CF 대기
        for _ in range(30):
            cf = await page.evaluate('document.body.innerText.includes("Just a moment")')
            if not cf:
                break
            await asyncio.sleep(0.5)
        html = await page.content()
        return html
    except Exception as e:
        return f'ERROR: {e}'
    finally:
        await context.close()


def run_async(coro):
    """비동기 코루틴을 동기적으로 실행"""
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=60)


class ProxyHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # /psa/{certNumber} — PSA cert 조회
        m = re.match(r'^/psa/(\d+)$', path)
        if m:
            cert_num = m.group(1)
            print(f'📋 PSA cert 조회: {cert_num}')
            try:
                info = run_async(fetch_psa_cert(cert_num))
                self._json_response(info)
            except Exception as e:
                self._json_response({'error': str(e)}, 500)
            return

        # /fetch?url=... — 범용 HTML 프록시
        if path == '/fetch':
            qs = parse_qs(parsed.query)
            target = qs.get('url', [None])[0]
            if not target:
                self._json_response({'error': 'url parameter required'}, 400)
                return
            print(f'🌐 URL 가져오기: {target[:80]}...')
            try:
                html = run_async(fetch_any_url(target))
                self.send_response(200)
                self._cors_headers()
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(html.encode('utf-8', errors='replace'))
            except Exception as e:
                self._json_response({'error': str(e)}, 500)
            return

        # /health — 헬스 체크
        if path == '/health':
            self._json_response({'status': 'ok', 'service': 'psa-proxy'})
            return

        # / — 안내
        self._json_response({
            'service': 'TCG Collector PSA Proxy',
            'endpoints': {
                '/psa/{certNumber}': 'PSA cert 정보 + 이미지 URL (JSON)',
                '/fetch?url=...': '범용 HTML 프록시 (Cloudflare 우회)',
                '/health': '헬스 체크',
            }
        })

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def _json_response(self, data, status=200):
        self.send_response(status)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        # 깔끔한 로그
        pass


def run_event_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main():
    global _loop

    # asyncio 이벤트 루프를 별도 스레드에서 실행
    _loop = asyncio.new_event_loop()
    t = Thread(target=run_event_loop, args=(_loop,), daemon=True)
    t.start()

    # 브라우저 초기화
    print('🚀 브라우저 초기화 중...')
    run_async(init_browser())
    print('✅ 브라우저 준비 완료')

    # HTTP 서버 시작
    server = HTTPServer(('127.0.0.1', PORT), ProxyHandler)
    print(f'🌐 PSA 프록시 서버 시작: http://localhost:{PORT}')
    print(f'   /psa/{{인증번호}} → PSA 카드 정보 조회')
    print(f'   /fetch?url=... → 범용 HTML 프록시')
    print(f'   Ctrl+C로 종료')
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n🛑 서버 종료 중...')
        server.shutdown()
        if _browser:
            run_async(_browser.close())
        if _playwright:
            run_async(_playwright.stop())
        _loop.call_soon_threadsafe(_loop.stop)
        print('👋 종료 완료')


if __name__ == '__main__':
    main()
