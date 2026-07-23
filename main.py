from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session
import asyncio
import warnings
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import binascii
import aiohttp
import requests
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
import time
from collections import defaultdict
from datetime import datetime, timedelta
import random
import os
import urllib.parse
import jwt
from functools import wraps
import secrets

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

KEY_LIMIT = 90
tracker = defaultdict(lambda: [0, time.time()])
liked_cache = defaultdict(set)
TOKEN_CACHE = {}
FAILED_ACCOUNTS = {}  # Track which accounts failed
FORCE_MODE = {}  # Track force mode per request

# EMON AXC API Base URL
EMON_API_BASE = "https://guild.emonaxc.com"

def get_today_midnight_timestamp():
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    return midnight.timestamp()

def load_accounts(server_name):
    try:
        if server_name == "IND":
            filename = "account_ind.txt"
        elif server_name in {"BR", "US", "SAC", "NA"}:
            filename = "account_br.txt"
        else:
            filename = "account_bd.txt"

        if not os.path.exists(filename):
            filename = "account_ind.txt"
            if not os.path.exists(filename):
                return []

        accounts = []
        with open(filename, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if ':' in line:
                    parts = line.split(':', 1)
                    uid = parts[0].strip()
                    password = parts[1].strip()
                    if uid and password:
                        accounts.append({"uid": uid, "password": password})
        return accounts
    except Exception as e:
        print(f"[ERROR] load_accounts: {e}")
        return []

async def generate_jwt_token(uid, password, max_retries=3):
    """Generate JWT token with retry logic"""
    encoded_password = urllib.parse.quote(password, safe='')
    url = f"https://ff-jwt-gen-api.lovable.app/api/public/token?uid={uid}&password={encoded_password}"

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
                        if isinstance(data, dict):
                            if 'jwt_token' in data:
                                return data['jwt_token']
                            elif 'token' in data:
                                return data['token']
                        print(f"[WARN] Token response format unexpected for {uid}: {data}")
                    else:
                        print(f"[WARN] Token API status {response.status} for {uid}")
        except asyncio.TimeoutError:
            print(f"[ERROR] Token timeout for {uid} (attempt {attempt+1}/{max_retries})")
        except Exception as e:
            print(f"[ERROR] Token generation failed for {uid}: {e}")

        if attempt < max_retries - 1:
            await asyncio.sleep(1)

    return None

async def get_valid_token(uid, password, force_refresh=False):
    """Get valid token with cache and refresh logic"""
    global TOKEN_CACHE

    if not force_refresh and uid in TOKEN_CACHE:
        cached = TOKEN_CACHE[uid]
        try:
            remaining = (cached["expires_at"] - datetime.utcnow()).total_seconds()
            if remaining > 300:
                print(f"[INFO] Using cached token for {uid} ({int(remaining)}s remaining)")
                return cached["token"]
            else:
                print(f"[INFO] Token for {uid} expiring soon ({int(remaining)}s), refreshing...")
        except Exception as e:
            print(f"[WARN] Cache check failed for {uid}: {e}")

    token = await generate_jwt_token(uid, password)
    if not token:
        print(f"[ERROR] Failed to generate token for {uid}")
        return None

    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        exp = payload.get("exp")
        if exp:
            TOKEN_CACHE[uid] = {
                "token": token,
                "expires_at": datetime.utcfromtimestamp(exp)
            }
            print(f"[INFO] Token cached for {uid}, expires at {datetime.utcfromtimestamp(exp)}")
        else:
            TOKEN_CACHE[uid] = {
                "token": token,
                "expires_at": datetime.utcnow() + timedelta(hours=24)
            }
    except Exception as e:
        print(f"[WARN] JWT decode failed for {uid}: {e}, using 24h fallback")
        TOKEN_CACHE[uid] = {
            "token": token,
            "expires_at": datetime.utcnow() + timedelta(hours=24)
        }
    return token

def encrypt_message(plaintext):
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_message = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded_message)).decode('utf-8')

def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()

async def send_like(encrypted_uid, token, url, max_retries=2):
    """Send like with retry logic"""
    edata = bytes.fromhex(encrypted_uid)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB54"
    }

    for attempt in range(max_retries):
        try:
            timeout = aiohttp.ClientTimeout(total=10, connect=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=edata, headers=headers) as response:
                    status = response.status
                    if status == 200:
                        return 200
                    elif status == 401:
                        print(f"[WARN] Token expired (401), will refresh")
                        return 401
                    else:
                        print(f"[WARN] Like API returned {status}")
                        return status
        except asyncio.TimeoutError:
            print(f"[ERROR] Like request timeout (attempt {attempt+1}/{max_retries})")
        except Exception as e:
            print(f"[ERROR] Like request failed: {e}")

        if attempt < max_retries - 1:
            await asyncio.sleep(0.5)

    return 500

async def process_account(target_uid, encrypted_uid, account, url, semaphore, server_name):
    """Process single account with full error tracking"""
    async with semaphore:
        uid = account['uid']
        password = account['password']

        # Try to get token (with force refresh on fail)
        token = await get_valid_token(uid, password)
        if not token:
            FAILED_ACCOUNTS[uid] = FAILED_ACCOUNTS.get(uid, 0) + 1
            print(f"[FAIL] {uid} - Token generation failed")
            return 500, uid, "token_failed"

        # Send like
        status = await send_like(encrypted_uid, token, url)

        if status == 200:
            liked_cache[target_uid].add(uid)
            print(f"[SUCCESS] {uid} - Like sent successfully")
            return status, uid, "success"
        elif status == 401:
            # Token expired, force refresh and retry once
            print(f"[RETRY] {uid} - Refreshing token and retrying...")
            token = await get_valid_token(uid, password, force_refresh=True)
            if token:
                status = await send_like(encrypted_uid, token, url)
                if status == 200:
                    liked_cache[target_uid].add(uid)
                    print(f"[SUCCESS] {uid} - Like sent after token refresh")
                    return status, uid, "success_retry"

            FAILED_ACCOUNTS[uid] = FAILED_ACCOUNTS.get(uid, 0) + 1
            print(f"[FAIL] {uid} - Failed even after token refresh")
            return 500, uid, "token_refresh_failed"
        else:
            FAILED_ACCOUNTS[uid] = FAILED_ACCOUNTS.get(uid, 0) + 1
            print(f"[FAIL] {uid} - Status {status}")
            return status, uid, f"http_{status}"

async def send_all_likes(target_uid, server_name, url, force=False):
    """Send likes from all accounts with detailed tracking"""
    region = server_name
    protobuf_message = create_protobuf_message(target_uid, region)
    encrypted_uid = encrypt_message(protobuf_message)

    accounts = load_accounts(server_name)
    if not accounts: 
        print(f"[ERROR] No accounts loaded for {server_name}")
        return {
            'success': 0, 
            'failed': 0, 
            'total': 0, 
            'already_liked': 0,
            'details': []
        }

    already_liked = liked_cache.get(target_uid, set())

    # FORCE MODE: Use failed accounts too
    if force:
        print(f"[FORCE MODE] Including previously failed accounts")
        fresh_accounts = accounts[:]  # Use ALL accounts
        # Remove only successfully liked ones
        fresh_accounts = [acc for acc in fresh_accounts if acc['uid'] not in already_liked]
    else:
        fresh_accounts = [acc for acc in accounts if acc['uid'] not in already_liked]

    print(f"[INFO] Total accounts: {len(accounts)}")
    print(f"[INFO] Already liked: {len(already_liked)}")
    print(f"[INFO] Fresh accounts: {len(fresh_accounts)}")

    if not fresh_accounts:
        print(f"[INFO] All accounts already liked this UID")
        return {
            'success': 0, 
            'failed': 0, 
            'total': len(accounts), 
            'already_liked': len(already_liked), 
            'fresh_used': 0,
            'details': []
        }

    random.shuffle(fresh_accounts)
    semaphore = asyncio.Semaphore(25)
    tasks = []

    for acc in fresh_accounts[:2000]:
        tasks.append(process_account(target_uid, encrypted_uid, acc, url, semaphore, server_name))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    successful = 0
    failed = 0
    details = []

    for r in results:
        if isinstance(r, tuple) and len(r) == 3:
            status, uid, reason = r
            details.append({'uid': uid, 'status': status, 'reason': reason})
            if status == 200:
                successful += 1
            else:
                failed += 1
        else:
            print(f"[ERROR] Unexpected result: {r}")
            failed += 1

    print(f"[SUMMARY] Success: {successful}, Failed: {failed}, Total: {len(fresh_accounts[:2000])}")

    if failed > 0:
        print(f"[FAILED ACCOUNTS] {dict(list(FAILED_ACCOUNTS.items())[-20:])}")

    return {
        'success': successful,
        'failed': failed,
        'total': len(accounts),
        'already_liked': len(already_liked),
        'fresh_used': len(fresh_accounts[:2000]),
        'details': details
    }

def enc(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return encrypt_message(message.SerializeToString())

def decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except Exception as e:
        print(f"[ERROR] Protobuf decode failed: {e}")
        return None

def get_player_info(encrypted_uid, server_name, token):
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        url = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_uid)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB54"
    }
    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        if response.status_code == 200:
            return decode_protobuf(response.content)
        else:
            print(f"[WARN] GetPlayerInfo status: {response.status_code}")
            return None
    except Exception as e:
        print(f"[ERROR] GetPlayerInfo failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# 🌐 FETCH PLAYER OUTFIT / DRESS INFO FROM EMON AXC API
# ═══════════════════════════════════════════════════════════════

def get_player_outfit_info(uid, region):
    """Fetch player outfit/dress info from EMON AXC API with multiple fallback endpoints"""
    endpoints = [
        f"{EMON_API_BASE}/info?uid={uid}&region={region}",
        f"{EMON_API_BASE}/player?uid={uid}&region={region}",
        f"{EMON_API_BASE}/api/info?uid={uid}&region={region}",
    ]

    for url in endpoints:
        try:
            resp = requests.get(url, timeout=15)
            print(f"[OUTFIT] Trying {url} -> Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                print(f"[OUTFIT] Data received: {json.dumps(data, indent=2)[:500]}")
                return data
        except Exception as e:
            print(f"[OUTFIT] Endpoint failed: {url} - {e}")

    return None


def get_player_outfit_image(uid, region):
    """Fetch player outfit image from EMON AXC API with multiple fallback endpoints"""
    endpoints = [
        f"{EMON_API_BASE}/outfit?uid={uid}&region={region}",
        f"{EMON_API_BASE}/image?uid={uid}&region={region}",
        f"{EMON_API_BASE}/api/outfit?uid={uid}&region={region}",
        f"{EMON_API_BASE}/dress?uid={uid}&region={region}",
    ]

    for url in endpoints:
        try:
            resp = requests.get(url, timeout=15)
            print(f"[OUTFIT_IMG] Trying {url} -> Status: {resp.status_code}")
            if resp.status_code == 200:
                # Check if response is an image
                content_type = resp.headers.get('Content-Type', '')
                if 'image' in content_type:
                    return url
                # If JSON with image URL
                try:
                    data = resp.json()
                    if 'image' in data:
                        return data['image']
                    if 'url' in data:
                        return data['url']
                    if 'outfit' in data and isinstance(data['outfit'], str):
                        return data['outfit']
                except:
                    pass
        except Exception as e:
            print(f"[OUTFIT_IMG] Endpoint failed: {url} - {e}")

    return None


def get_player_outfit_fallback(uid, region, player_name=""):
    """Fallback outfit data using player info"""
    return {
        "level": "N/A",
        "likes": "N/A", 
        "rank": "N/A",
        "region": region,
        "uid": uid,
        "name": player_name,
        "outfit_image_url": None
    }


# ═══════════════════════════════════════════════════════════════
# 🌐 PROFESSIONAL DASHBOARD (NO LOGIN REQUIRED)
# ═══════════════════════════════════════════════════════════════

PANEL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SAMIUL | Free Fire Like Sender</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body {
            font-family: 'Rajdhani', sans-serif;
            background: #050505;
            min-height: 100vh;
            color: #fff;
            overflow-x: hidden;
            -webkit-font-smoothing: antialiased;
        }
        .bg-grid {
            position: fixed; top: 0; left: 0;
            width: 100%; height: 100%;
            background-image: 
                linear-gradient(rgba(255,107,53,0.02) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,107,53,0.02) 1px, transparent 1px);
            background-size: 40px 40px;
            pointer-events: none; z-index: 0;
        }
        .glow-orb {
            position: fixed; width: 350px; height: 350px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(255,107,53,0.08), transparent 70%);
            pointer-events: none; z-index: 0;
            animation: orbFloat 10s ease-in-out infinite;
        }
        .glow-orb:nth-child(1) { top: -150px; right: -150px; }
        .glow-orb:nth-child(2) { bottom: -150px; left: -150px; animation-delay: -5s; }
        @keyframes orbFloat {
            0%, 100% { transform: translate(0,0) scale(1); }
            50% { transform: translate(-20px, 20px) scale(1.1); }
        }

        .navbar {
            position: fixed; top: 0; left: 0; right: 0;
            background: rgba(10,10,10,0.95);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-bottom: 1px solid rgba(255,107,53,0.1);
            z-index: 100; padding: 0 16px;
            height: 56px; display: flex;
            align-items: center; justify-content: space-between;
        }
        .nav-brand {
            display: flex; align-items: center; gap: 10px;
        }
        .nav-logo {
            width: 34px; height: 34px; border-radius: 8px;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            display: flex; align-items: center; justify-content: center;
            font-size: 16px; box-shadow: 0 0 12px rgba(255,107,53,0.3);
        }
        .nav-title {
            font-family: 'Orbitron', sans-serif;
            font-size: 14px; font-weight: 900;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            letter-spacing: 1px;
        }
        .nav-tag { color: #555; font-size: 9px; letter-spacing: 1px; text-transform: uppercase; }

        .main-content {
            padding: 76px 12px 30px;
            max-width: 600px; margin: 0 auto;
            position: relative; z-index: 1;
        }
        .page-header {
            text-align: center; margin-bottom: 24px;
        }
        .page-header h1 {
            font-family: 'Orbitron', sans-serif;
            font-size: 22px; font-weight: 900;
            background: linear-gradient(135deg, #ff6b35, #f7931e, #ffcc00);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            letter-spacing: 2px; margin-bottom: 4px;
        }
        .page-header p { color: #555; font-size: 12px; letter-spacing: 1px; }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px; margin-bottom: 20px;
        }
        .stat-card {
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 14px;
            padding: 14px 8px;
            position: relative; overflow: hidden;
            transition: all 0.3s;
            text-align: center;
        }
        .stat-card:hover {
            border-color: rgba(255,107,53,0.2);
        }
        .stat-card::before {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
            background: linear-gradient(90deg, transparent, #ff6b35, #f7931e, transparent);
        }
        .stat-label {
            color: #555; font-size: 9px;
            text-transform: uppercase; letter-spacing: 1px;
            font-weight: 700; margin-bottom: 6px;
        }
        .stat-value {
            font-family: 'Orbitron', sans-serif;
            font-size: 20px; font-weight: 900;
            color: #fff;
        }
        .stat-value.accent {
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }

        .main-card {
            background: rgba(15,15,15,0.98);
            border: 1px solid rgba(255,107,53,0.12);
            border-radius: 20px;
            padding: 24px 16px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.6);
            position: relative; overflow: hidden;
        }
        .main-card::after {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
            background: linear-gradient(90deg, transparent, #ff6b35, #f7931e, transparent);
        }
        .card-title {
            font-family: 'Orbitron', sans-serif;
            font-size: 12px; color: #ff6b35;
            text-transform: uppercase; letter-spacing: 2px;
            margin-bottom: 18px; display: flex;
            align-items: center; gap: 8px;
        }
        .card-title::before {
            content: ''; width: 3px; height: 16px;
            background: linear-gradient(180deg, #ff6b35, #f7931e);
            border-radius: 2px;
        }

        .form-row {
            display: grid;
            grid-template-columns: 1.8fr 1fr;
            gap: 10px; margin-bottom: 16px;
        }
        @media (max-width: 400px) {
            .form-row { grid-template-columns: 1fr; }
        }
        .input-group { position: relative; }
        .input-group label {
            display: block; color: #ff6b35;
            font-weight: 700; font-size: 9px;
            text-transform: uppercase; letter-spacing: 1px;
            margin-bottom: 6px; font-family: 'Orbitron', sans-serif;
        }
        .input-field {
            width: 100%; padding: 12px 14px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px; color: #fff;
            font-family: 'Rajdhani', sans-serif;
            font-size: 14px; font-weight: 500;
            transition: all 0.3s ease; outline: none;
            -webkit-appearance: none;
        }
        .input-field:focus {
            border-color: #ff6b35;
            box-shadow: 0 0 0 3px rgba(255,107,53,0.1), 0 0 15px rgba(255,107,53,0.1);
            background: rgba(255,255,255,0.05);
        }
        .input-field::placeholder { color: #333; }
        select.input-field {
            cursor: pointer;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='%23ff6b35' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 10px center;
            background-size: 16px;
            padding-right: 34px;
        }

        .btn-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px; margin-top: 4px;
        }
        .send-btn {
            width: 100%; padding: 14px;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            border: none; border-radius: 12px;
            color: #000; font-family: 'Orbitron', sans-serif;
            font-size: 13px; font-weight: 900;
            text-transform: uppercase; letter-spacing: 2px;
            cursor: pointer; transition: all 0.3s ease;
            position: relative; overflow: hidden;
            -webkit-appearance: none;
        }
        .send-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(255,107,53,0.3);
        }
        .send-btn::after {
            content: ''; position: absolute;
            top: -50%; left: -50%; width: 200%; height: 200%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent);
            transform: rotate(30deg); transition: all 0.6s;
        }
        .send-btn:hover::after { left: 100%; }
        .send-btn:disabled {
            opacity: 0.5; cursor: not-allowed;
            transform: none;
        }
        .send-btn:active {
            transform: scale(0.98);
        }

        .force-btn {
            width: 100%; padding: 14px;
            background: linear-gradient(135deg, #ff4444, #ff6b6b);
            border: none; border-radius: 12px;
            color: #fff; font-family: 'Orbitron', sans-serif;
            font-size: 13px; font-weight: 900;
            text-transform: uppercase; letter-spacing: 2px;
            cursor: pointer; transition: all 0.3s ease;
            position: relative; overflow: hidden;
            -webkit-appearance: none;
        }
        .force-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(255,68,68,0.3);
        }
        .force-btn:disabled {
            opacity: 0.5; cursor: not-allowed;
            transform: none;
        }

        .loading {
            display: none; text-align: center;
            margin-top: 20px;
        }
        .loading.active { display: block; }
        .spinner {
            width: 36px; height: 36px;
            border: 3px solid rgba(255,107,53,0.15);
            border-top-color: #ff6b35;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 10px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading-text {
            color: #ff6b35; font-family: 'Orbitron', sans-serif;
            font-size: 11px; letter-spacing: 2px;
        }

        .player-info-card {
            display: none; margin-top: 20px;
            background: rgba(255,107,53,0.04);
            border: 1px solid rgba(255,107,53,0.15);
            border-radius: 16px; padding: 16px;
            animation: slideUp 0.5s ease;
        }
        .player-info-card.active { display: block; }
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .player-info-header {
            display: flex; align-items: center; gap: 12px;
            margin-bottom: 14px; padding-bottom: 12px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .player-avatar {
            width: 56px; height: 56px; border-radius: 14px;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            display: flex; align-items: center; justify-content: center;
            font-size: 24px; flex-shrink: 0;
            box-shadow: 0 0 20px rgba(255,107,53,0.2);
        }
        .player-name-section h3 {
            font-family: 'Orbitron', sans-serif;
            color: #fff; font-size: 15px;
            letter-spacing: 1px;
        }
        .player-name-section .player-uid {
            color: #555; font-size: 11px;
            font-family: 'Orbitron', sans-serif;
        }

        .outfit-section {
            margin-top: 14px;
        }
        .outfit-title {
            font-family: 'Orbitron', sans-serif;
            font-size: 10px; color: #ff6b35;
            text-transform: uppercase; letter-spacing: 2px;
            margin-bottom: 10px;
        }
        .outfit-image-container {
            width: 100%; max-width: 280px;
            margin: 0 auto 12px;
            border-radius: 16px;
            overflow: hidden;
            border: 1px solid rgba(255,107,53,0.2);
            background: rgba(0,0,0,0.3);
            position: relative;
        }
        .outfit-image-container img {
            width: 100%; height: auto;
            display: block;
        }
        .outfit-image-placeholder {
            width: 100%; aspect-ratio: 1;
            display: flex; align-items: center; justify-content: center;
            color: #333; font-size: 12px;
            min-height: 200px;
        }
        .outfit-details {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
        }
        .outfit-item {
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.04);
            border-radius: 10px; padding: 10px 12px;
        }
        .outfit-item-label {
            color: #444; font-size: 9px;
            text-transform: uppercase; letter-spacing: 1px;
            margin-bottom: 3px;
        }
        .outfit-item-value {
            color: #fff; font-weight: 700;
            font-size: 13px; font-family: 'Orbitron', sans-serif;
        }

        .result-card {
            display: none; margin-top: 16px;
            background: rgba(255,107,53,0.04);
            border: 1px solid rgba(255,107,53,0.15);
            border-radius: 16px; padding: 16px;
            animation: slideUp 0.5s ease;
        }
        .result-card.active { display: block; }
        .result-header {
            display: flex; align-items: center; gap: 8px;
            margin-bottom: 12px; padding-bottom: 10px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .result-header h3 {
            font-family: 'Orbitron', sans-serif;
            color: #ff6b35; font-size: 11px;
            text-transform: uppercase; letter-spacing: 2px;
        }
        .result-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
        }
        @media (min-width: 500px) {
            .result-grid { grid-template-columns: repeat(3, 1fr); }
        }
        .result-item {
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.04);
            border-radius: 10px; padding: 10px 12px;
        }
        .result-label { color: #444; font-size: 9px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 3px; }
        .result-value {
            color: #fff; font-weight: 700;
            font-size: 14px; font-family: 'Orbitron', sans-serif;
        }
        .result-value.success { color: #00ff88; }
        .result-value.error { color: #ff4444; }
        .result-value.warning { color: #ffcc00; }
        .status-badge {
            display: inline-block; padding: 3px 10px;
            border-radius: 20px; font-size: 9px;
            font-weight: 700; text-transform: uppercase;
        }
        .status-success { background: rgba(0,255,136,0.1); color: #00ff88; border: 1px solid rgba(0,255,136,0.2); }
        .status-failed { background: rgba(255,68,68,0.1); color: #ff4444; border: 1px solid rgba(255,68,68,0.2); }
        .status-pending { background: rgba(255,204,0,0.1); color: #ffcc00; border: 1px solid rgba(255,204,0,0.2); }

        .error-msg {
            display: none; margin-top: 14px;
            padding: 12px 14px;
            background: rgba(255,68,68,0.06);
            border: 1px solid rgba(255,68,68,0.15);
            border-radius: 12px; color: #ff4444;
            text-align: center; font-weight: 600;
            font-size: 12px; animation: shake 0.4s ease;
        }
        .error-msg.active { display: block; }
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-6px); }
            75% { transform: translateX(6px); }
        }

        .footer {
            text-align: center; margin-top: 30px;
            color: #222; font-size: 10px;
            letter-spacing: 1px; padding-bottom: 20px;
        }
        .footer span { color: #ff6b35; font-weight: 700; }
        .footer .dev { color: #333; margin-top: 3px; font-size: 9px; }

        .debug-panel {
            display: none;
            margin-top: 20px;
            background: rgba(0,0,0,0.8);
            border: 1px solid rgba(255,107,53,0.2);
            border-radius: 12px;
            padding: 16px;
            font-family: monospace;
            font-size: 11px;
            color: #aaa;
            max-height: 300px;
            overflow-y: auto;
        }
        .debug-panel.active { display: block; }
        .debug-line { margin-bottom: 4px; }
        .debug-success { color: #00ff88; }
        .debug-fail { color: #ff4444; }
        .debug-info { color: #ff6b35; }

        .force-badge {
            display: inline-block;
            background: linear-gradient(135deg, #ff4444, #ff6b6b);
            color: #fff;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 9px;
            font-weight: 700;
            margin-left: 8px;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }

        @media (max-width: 480px) {
            .main-content { padding: 70px 10px 20px; }
            .page-header h1 { font-size: 18px; }
            .stat-value { font-size: 16px; }
            .stat-card { padding: 12px 6px; }
            .main-card { padding: 20px 14px; border-radius: 16px; }
            .player-avatar { width: 48px; height: 48px; font-size: 20px; }
            .outfit-image-container { max-width: 240px; }
            .btn-row { grid-template-columns: 1fr; }
        }
        @media (max-width: 360px) {
            .stats-grid { gap: 6px; }
            .stat-label { font-size: 8px; }
            .stat-value { font-size: 14px; }
            .result-grid { grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>
    <div class="bg-grid"></div>
    <div class="glow-orb"></div>
    <div class="glow-orb"></div>

    <nav class="navbar">
        <div class="nav-brand">
            <div class="nav-logo">🔥</div>
            <div>
                <div class="nav-title">SAMIUL</div>
                <div class="nav-tag">Free Fire Like Sender</div>
            </div>
        </div>
    </nav>

    <div class="main-content">
        <div class="page-header">
            <h1>Like Sender</h1>
            <p>Enter player UID & region to send likes</p>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">Daily Limit</div>
                <div class="stat-value accent" id="statLimit">90</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Used Today</div>
                <div class="stat-value" id="statUsed">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Remaining</div>
                <div class="stat-value accent" id="statRemaining">90</div>
            </div>
        </div>

        <div class="main-card">
            <div class="card-title">Send Likes</div>

            <div class="form-row">
                <div class="input-group">
                    <label>🎮 Player UID</label>
                    <input type="text" class="input-field" id="uid" placeholder="Enter Free Fire UID" maxlength="20" inputmode="numeric">
                </div>
                <div class="input-group">
                    <label>🌍 Region</label>
                    <select class="input-field" id="region">
                        <option value="IND">🇮🇳 India</option>
                        <option value="BR">🇧🇷 Brazil</option>
                        <option value="US">🇺🇸 United States</option>
                        <option value="SAC">🌎 South America</option>
                        <option value="NA">🇨🇦 North America</option>
                        <option value="BD">🇧🇩 Bangladesh</option>
                        <option value="RU">🇷🇺 Russia</option>
                    </select>
                </div>
            </div>

            <div class="btn-row">
                <button class="send-btn" id="sendBtn">
                    ⚡ SEND LIKES
                </button>
                <button class="force-btn" id="forceBtn">
                    🔥 FORCE SEND
                </button>
            </div>

            <div class="loading" id="loading">
                <div class="spinner"></div>
                <p class="loading-text">PROCESSING REQUEST...</p>
            </div>

            <div class="error-msg" id="errorMsg"></div>

            <div class="player-info-card" id="playerInfoCard">
                <div class="player-info-header">
                    <div class="player-avatar">👤</div>
                    <div class="player-name-section">
                        <h3 id="playerNickname">Player Name</h3>
                        <div class="player-uid" id="playerUidText">UID: --</div>
                    </div>
                </div>

                <div class="outfit-section" id="outfitSection">
                    <div class="outfit-title">👗 Player Outfit <span id="forceBadge" class="force-badge" style="display:none;">FORCE MODE</span></div>
                    <div class="outfit-image-container" id="outfitImageContainer">
                        <div class="outfit-image-placeholder">Loading outfit...</div>
                    </div>
                    <div class="outfit-details" id="outfitDetails"></div>
                </div>
            </div>

            <div class="result-card" id="resultCard">
                <div class="result-header">
                    <h3>📊 Like Results</h3>
                </div>
                <div class="result-grid" id="resultContent"></div>
            </div>

            <div class="debug-panel" id="debugPanel">
                <div style="color:#ff6b35; font-weight:bold; margin-bottom:10px;">🔧 Account Debug Log</div>
                <div id="debugContent"></div>
            </div>
        </div>
    </div>

    <div class="footer">
        <span>🔥 SAMIUL API</span> — All rights reserved
        <div class="dev">Developed by SAMIUL</div>
    </div>

    <script>
        const API_KEY = "JMLB";

        function resetUI() {
            document.getElementById('loading').classList.remove('active');
            document.getElementById('errorMsg').classList.remove('active');
            document.getElementById('resultCard').classList.remove('active');
            document.getElementById('playerInfoCard').classList.remove('active');
            document.getElementById('debugPanel').classList.remove('active');
            document.getElementById('debugContent').innerHTML = '';
            document.getElementById('forceBadge').style.display = 'none';
            const sendBtn = document.getElementById('sendBtn');
            const forceBtn = document.getElementById('forceBtn');
            sendBtn.disabled = false;
            sendBtn.textContent = '⚡ SEND LIKES';
            forceBtn.disabled = false;
            forceBtn.textContent = '🔥 FORCE SEND';
        }

        function showError(msg) {
            const el = document.getElementById('errorMsg');
            el.textContent = msg;
            el.classList.add('active');
            setTimeout(() => el.classList.remove('active'), 8000);
        }

        function updateStats(used, limit, remaining) {
            document.getElementById('statUsed').textContent = used || 0;
            document.getElementById('statRemaining').textContent = remaining !== undefined ? remaining : (limit - (used||0));
            document.getElementById('statLimit').textContent = limit;
        }

        function displayDebug(details) {
            const panel = document.getElementById('debugPanel');
            const content = document.getElementById('debugContent');
            let html = '';
            details.forEach(d => {
                const cls = d.status === 200 ? 'debug-success' : 'debug-fail';
                const statusText = d.status === 200 ? '✅' : '❌';
                html += `<div class="debug-line ${cls}">${statusText} ${d.uid} — ${d.reason} (HTTP ${d.status})</div>`;
            });
            content.innerHTML = html;
            panel.classList.add('active');
        }

        function displayPlayerInfo(data, isForce) {
            const card = document.getElementById('playerInfoCard');
            document.getElementById('playerNickname').textContent = data.nickname || 'Unknown Player';
            document.getElementById('playerUidText').textContent = 'UID: ' + (data.uid || '--');

            if (isForce) {
                document.getElementById('forceBadge').style.display = 'inline-block';
            }

            const imgContainer = document.getElementById('outfitImageContainer');
            if (data.outfit_image_url) {
                imgContainer.innerHTML = `<img src="${data.outfit_image_url}" alt="Player Outfit" onerror="this.parentElement.innerHTML='<div class=\'outfit-image-placeholder\'>❌ Outfit image failed to load</div>'">`;
            } else {
                // Try to generate a fallback outfit image URL
                const fallbackUrl = `https://guild.emonaxc.com/outfit?uid=${data.uid}&region=${data.region}`;
                imgContainer.innerHTML = `<img src="${fallbackUrl}" alt="Player Outfit" onerror="this.parentElement.innerHTML='<div class=\'outfit-image-placeholder\'>👕 No outfit image available</div>'">`;
            }

            const detailsContainer = document.getElementById('outfitDetails');
            let detailsHTML = '';
            if (data.outfit_info && typeof data.outfit_info === 'object') {
                const info = data.outfit_info;
                // Try multiple possible field names
                const level = info.level || info.Level || info.playerLevel || info['Player Level'] || 'N/A';
                const likes = info.likes || info.Likes || info.totalLikes || info['Total Likes'] || data.likes_after || 'N/A';
                const rank = info.rank || info.Rank || info.playerRank || info['Player Rank'] || 'N/A';
                const region = info.region || info.Region || data.region || 'N/A';

                const fields = [
                    { val: level, label: 'Level', icon: '⭐' },
                    { val: likes, label: 'Likes', icon: '❤️' },
                    { val: rank, label: 'Rank', icon: '🏆' },
                    { val: region, label: 'Region', icon: '🌍' },
                ];
                fields.forEach(f => {
                    if (f.val !== undefined && f.val !== null && f.val !== 'N/A') {
                        detailsHTML += `
                            <div class="outfit-item">
                                <div class="outfit-item-label">${f.icon} ${f.label}</div>
                                <div class="outfit-item-value">${f.val}</div>
                            </div>
                        `;
                    }
                });

                // Add any other fields from API
                Object.keys(info).forEach(key => {
                    if (!['level','Level','likes','Likes','rank','Rank','region','Region'].includes(key)) {
                        const val = info[key];
                        if (val !== undefined && val !== null && val !== '') {
                            detailsHTML += `
                                <div class="outfit-item">
                                    <div class="outfit-item-label">📋 ${key}</div>
                                    <div class="outfit-item-value">${val}</div>
                                </div>
                            `;
                        }
                    }
                });
            }
            if (!detailsHTML) {
                detailsHTML = `
                    <div class="outfit-item">
                        <div class="outfit-item-label">🌍 Region</div>
                        <div class="outfit-item-value">${data.region || 'N/A'}</div>
                    </div>
                    <div class="outfit-item">
                        <div class="outfit-item-label">❤️ Likes</div>
                        <div class="outfit-item-value">${data.likes_after || 0}</div>
                    </div>
                `;
            }
            detailsContainer.innerHTML = detailsHTML;
            card.classList.add('active');
        }

        function displayLikeResult(data, isForce) {
            const content = document.getElementById('resultContent');
            let sc = 'warning', st = 'PENDING';
            if (data.status === 1) { sc = 'success'; st = 'SUCCESS'; }
            else if (data.status === 2) { sc = 'warning'; st = 'NO CHANGE'; }
            else if (data.status === 0) { sc = 'error'; st = 'FAILED'; }

            content.innerHTML = `
                <div class="result-item">
                    <div class="result-label">Nickname</div>
                    <div class="result-value">${data.PlayerNickname || 'N/A'}</div>
                </div>
                <div class="result-item">
                    <div class="result-label">UID</div>
                    <div class="result-value">${data.UID || 'N/A'}</div>
                </div>
                <div class="result-item">
                    <div class="result-label">Likes Before</div>
                    <div class="result-value">${data.LikesbeforeCommand || 0}</div>
                </div>
                <div class="result-item">
                    <div class="result-label">Likes After</div>
                    <div class="result-value">${data.LikesafterCommand || 0}</div>
                </div>
                <div class="result-item">
                    <div class="result-label">Likes Given</div>
                    <div class="result-value success">+${data.LikesGivenByAPI || 0}</div>
                </div>
                <div class="result-item">
                    <div class="result-label">Success/Failed</div>
                    <div class="result-value">${data.success_count || 0}/${data.failed_count || 0}</div>
                </div>
                <div class="result-item">
                    <div class="result-label">Mode</div>
                    <div class="result-value ${isForce ? 'error' : 'success'}">${isForce ? '🔥 FORCE' : '⚡ NORMAL'}</div>
                </div>
                <div class="result-item">
                    <div class="result-label">Status</div>
                    <div><span class="status-badge status-${sc}">${st}</span></div>
                </div>
            `;
            document.getElementById('resultCard').classList.add('active');
        }

        async function sendLike(force = false) {
            const uid = document.getElementById('uid').value.trim();
            const region = document.getElementById('region').value;
            const sendBtn = document.getElementById('sendBtn');
            const forceBtn = document.getElementById('forceBtn');
            const loading = document.getElementById('loading');

            resetUI();

            if (!uid) { showError('❌ Please enter a valid UID!'); return; }
            if (!/^\d+$/.test(uid)) { showError('❌ UID must contain only numbers!'); return; }

            sendBtn.disabled = true;
            forceBtn.disabled = true;
            sendBtn.textContent = force ? '🔥 FORCING...' : '⏳ PROCESSING...';
            forceBtn.textContent = force ? '🔥 FORCING...' : '🔥 FORCE SEND';
            loading.classList.add('active');

            try {
                const forceParam = force ? '&force=1' : '';
                const apiUrl = `/like?key=${API_KEY}&uid=${encodeURIComponent(uid)}&server_name=${encodeURIComponent(region)}${forceParam}`;
                const resp = await fetch(apiUrl);
                const data = await resp.json();

                resetUI();

                if (data.error) {
                    showError(`❌ ${data.error}`);
                    return;
                }

                displayPlayerInfo({
                    nickname: data.PlayerNickname,
                    uid: data.UID,
                    region: region,
                    likes_after: data.LikesafterCommand,
                    outfit_image_url: data.outfit_image_url,
                    outfit_info: data.outfit_info
                }, force);

                displayLikeResult(data, force);

                if (data.details && data.details.length > 0) {
                    displayDebug(data.details);
                }

                const remainsMatch = data.remains ? data.remains.match(/\d+/) : null;
                const used = remainsMatch ? (90 - parseInt(remainsMatch[0])) : 0;
                updateStats(used, 90, remainsMatch ? parseInt(remainsMatch[0]) : 90);

            } catch (e) {
                console.error(e);
                resetUI();
                showError('❌ Network error! Please try again.');
            }
        }

        document.getElementById('sendBtn').addEventListener('click', () => sendLike(false));
        document.getElementById('forceBtn').addEventListener('click', () => sendLike(true));
        document.getElementById('uid').addEventListener('keypress', e => {
            if (e.key === 'Enter') sendLike(false);
        });
    </script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def home():
    return render_template_string(PANEL_TEMPLATE)


# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route('/like', methods=['GET'])
def handle_requests():
    uid = request.args.get("uid")
    server_name = request.args.get("server_name", "").upper()
    key = request.args.get("key")
    force = request.args.get("force", "0") == "1"
    client_ip = request.remote_addr

    if key != "JMLB":
        return jsonify({"error": "Invalid or missing API key 🔑"}), 403

    if not uid or not server_name:
        return jsonify({"error": "UID and server_name are required"}), 400

    valid_servers = ["IND", "BR", "US", "SAC", "NA", "BD", "RU"]
    if server_name not in valid_servers:
        return jsonify({"error": f"Invalid server. Use: {valid_servers}"}), 400

    accounts = load_accounts(server_name)
    if not accounts:
        accounts = load_accounts("IND")
        if not accounts:
            return jsonify({"error": f"No accounts found for server {server_name}"}), 500

    today_midnight = get_today_midnight_timestamp()
    count, last_reset = tracker[client_ip]

    if last_reset < today_midnight:
        tracker[client_ip] = [0, time.time()]
        count = 0

    if count >= KEY_LIMIT:
        return jsonify({"error": "Daily limit reached", "remains": f"(0/{KEY_LIMIT})"}), 429

    check_token = None
    for account in accounts[:5]:
        check_token = asyncio.run(get_valid_token(account['uid'], account['password']))
        if check_token:
            break

    if not check_token:
        return jsonify({"error": "Token generation failed - no valid accounts"}), 500

    encrypted_uid = enc(uid)

    before = get_player_info(encrypted_uid, server_name, check_token)
    if before is None:
        return jsonify({"error": "Invalid UID or server", "status": 0}), 200

    try:
        before_data = json.loads(MessageToJson(before))
        before_like = int(before_data['AccountInfo'].get('Likes', 0))
    except Exception as e:
        print(f"[ERROR] Before data parse failed: {e}")
        return jsonify({"error": "Data parsing failed", "status": 0}), 200

    if server_name == "IND":
        like_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        like_url = "https://client.us.freefiremobile.com/LikeProfile"
    else:
        like_url = "https://clientbp.ggpolarbear.com/LikeProfile"

    result = asyncio.run(send_all_likes(uid, server_name, like_url, force=force))

    after = get_player_info(encrypted_uid, server_name, check_token)
    if after is None:
        return jsonify({"error": "Could not verify likes after command", "status": 0}), 200

    try:
        after_data = json.loads(MessageToJson(after))
        after_like = int(after_data['AccountInfo']['Likes'])
        player_id = int(after_data['AccountInfo']['UID'])
        player_name = str(after_data['AccountInfo']['PlayerNickname'])

        like_given = after_like - before_like
        status = 1 if like_given != 0 else 2

        if like_given > 0:
            tracker[client_ip][0] += 1
            count += 1

        remains = KEY_LIMIT - count

        # Fetch outfit info from EMON AXC API with better handling
        outfit_data = get_player_outfit_info(uid, server_name)
        outfit_image_url = get_player_outfit_image(uid, server_name)

        # If API returns nothing, create fallback
        if not outfit_data:
            outfit_data = get_player_outfit_fallback(uid, server_name, player_name)

        # Ensure outfit_data has basic fields
        if isinstance(outfit_data, dict):
            outfit_data['uid'] = uid
            outfit_data['region'] = server_name
            outfit_data['name'] = player_name

        print(f"[RESPONSE] outfit_data: {outfit_data}")
        print(f"[RESPONSE] outfit_image_url: {outfit_image_url}")

        response_data = {
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": after_like,
            "LikesbeforeCommand": before_like,
            "PlayerNickname": player_name,
            "UID": player_id,
            "status": status,
            "remains": f"({remains}/{KEY_LIMIT})",
            "outfit_image_url": outfit_image_url,
            "outfit_info": outfit_data,
            "success_count": result['success'],
            "failed_count": result['failed'],
            "details": result.get('details', [])
        }

        return jsonify(response_data)
    except Exception as e:
        print(f"[ERROR] Final response error: {e}")
        return jsonify({"error": str(e), "status": 0}), 500


@app.route('/reset-cache', methods=['GET'])
def reset_cache():
    key = request.args.get("key")
    if key != "JMLB":
        return jsonify({"error": "Invalid key"}), 403

    global liked_cache, FAILED_ACCOUNTS
    liked_cache.clear()
    FAILED_ACCOUNTS.clear()
    return jsonify({"message": "Cache cleared", "credit": "Samiul"})


if __name__ == '__main__':
    print("🚀 SAMIUL Free Fire Like Sender Server Started!")
    print("📁 Required account files:")
    print("   - account_ind.txt (IND server)")
    print("   - account_br.txt (BR/US/SAC/NA servers)")
    print("   - account_bd.txt (BD/RU server)")
    print("🔓 No login required - Direct access")
    print("🌐 Open http://localhost:5000 in your browser")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
