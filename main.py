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
GOOGLE_CLIENT_ID = "YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com"
GOOGLE_CLIENT_SECRET = "YOUR_GOOGLE_CLIENT_SECRET"
GOOGLE_REDIRECT_URI = "http://localhost:5000/google/callback"

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

KEY_LIMIT = 90
tracker = defaultdict(lambda: [0, time.time()])
liked_cache = defaultdict(set)
TOKEN_CACHE = {}

# ═══════════════════════════════════════════════════════════════
# AUTH DECORATOR - LOGIN REQUIRED (NO GUEST ACCESS)
# ═══════════════════════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect('/')
        return f(*args, **kwargs)
    return decorated_function

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
    except:
        return []

async def generate_jwt_token(uid, password):
    try:
        encoded_password = urllib.parse.quote(password)
        url = f"https://ff-jwt-gen-api.lovable.app/api/public/token?uid={uid}&password={encoded_password}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=24) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict):
                        if 'jwt_token' in data:
                            return data['jwt_token']
                        elif 'token' in data:
                            return data['token']
                return None
    except:
        return None

async def get_valid_token(uid, password):
    if uid in TOKEN_CACHE:
        cached = TOKEN_CACHE[uid]
        remaining = (cached["expires_at"] - datetime.utcnow()).total_seconds()
        if remaining > 1800:
            return cached["token"]

    token = await generate_jwt_token(uid, password)
    if not token:
        return None

    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        exp = payload.get("exp")
        TOKEN_CACHE[uid] = {
            "token": token,
            "expires_at": datetime.utcfromtimestamp(exp)
        }
    except:
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

async def send_like(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/x-www-form-urlencoded",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB54"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers, timeout=5) as response:
                return response.status
    except:
        return 500

async def process_account(target_uid, encrypted_uid, account, url, semaphore, server_name):
    async with semaphore:
        token = await get_valid_token(account['uid'], account['password'])
        if not token:
            return 500, account['uid']
        status = await send_like(encrypted_uid, token, url)
        if status == 200:
            liked_cache[target_uid].add(account['uid'])
            return status, account['uid']
        return status, account['uid']

async def send_all_likes(target_uid, server_name, url):
    region = server_name
    protobuf_message = create_protobuf_message(target_uid, region)
    encrypted_uid = encrypt_message(protobuf_message)

    accounts = load_accounts(server_name)
    if not accounts: 
        return {'success': 0, 'failed': 0, 'total': 0, 'already_liked': 0}

    already_liked = liked_cache.get(target_uid, set())
    fresh_accounts = [acc for acc in accounts if acc['uid'] not in already_liked]

    if not fresh_accounts:
        return {'success': 0, 'failed': 0, 'total': len(accounts), 'already_liked': len(already_liked), 'fresh_used': 0}

    random.shuffle(fresh_accounts)
    semaphore = asyncio.Semaphore(25)
    tasks = []
    for acc in fresh_accounts[:2000]:
        tasks.append(process_account(target_uid, encrypted_uid, acc, url, semaphore, server_name))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    successful = 0
    failed = 0
    for r in results:
        if isinstance(r, tuple):
            status, uid = r
            if status == 200:
                successful += 1
            else:
                failed += 1

    return {
        'success': successful,
        'failed': failed,
        'total': len(accounts),
        'already_liked': len(already_liked),
        'fresh_used': len(fresh_accounts[:2000])
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
    except:
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
        return decode_protobuf(response.content)
    except:
        return None


# ═══════════════════════════════════════════════════════════════
# 🌐 PROFESSIONAL LOGIN PAGE
# ═══════════════════════════════════════════════════════════════

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SAMIUL | Free Fire Like Sender - Login</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Rajdhani', sans-serif;
            background: #050505;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            overflow: hidden;
            position: relative;
        }
        .bg-grid {
            position: fixed; top: 0; left: 0;
            width: 100%; height: 100%;
            background-image: 
                linear-gradient(rgba(255,107,53,0.03) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,107,53,0.03) 1px, transparent 1px);
            background-size: 50px 50px;
            pointer-events: none; z-index: 0;
        }
        .glow-orb {
            position: fixed; width: 400px; height: 400px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(255,107,53,0.15), transparent 70%);
            pointer-events: none; z-index: 0;
            animation: orbFloat 8s ease-in-out infinite;
        }
        .glow-orb:nth-child(1) { top: -100px; left: -100px; }
        .glow-orb:nth-child(2) { bottom: -100px; right: -100px; animation-delay: -4s; }
        @keyframes orbFloat {
            0%, 100% { transform: translate(0, 0) scale(1); }
            50% { transform: translate(30px, -30px) scale(1.1); }
        }
        .main-container {
            position: relative; z-index: 1;
            width: 100%; max-width: 440px;
        }
        .card {
            background: rgba(15, 15, 15, 0.98);
            border: 1px solid rgba(255, 107, 53, 0.15);
            border-radius: 24px;
            padding: 45px 40px;
            box-shadow: 
                0 0 0 1px rgba(255,107,53,0.05),
                0 20px 60px rgba(0,0,0,0.8),
                0 0 100px rgba(255,107,53,0.08);
            backdrop-filter: blur(20px);
            position: relative; overflow: hidden;
        }
        .card::after {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px;
            background: linear-gradient(90deg, transparent, #ff6b35, #f7931e, #ff6b35, transparent);
            animation: scanline 3s ease-in-out infinite;
        }
        @keyframes scanline {
            0%, 100% { opacity: 0.3; }
            50% { opacity: 1; }
        }
        .logo-section { text-align: center; margin-bottom: 35px; }
        .logo-ring {
            width: 80px; height: 80px;
            margin: 0 auto 15px;
            border-radius: 50%;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            display: flex; align-items: center; justify-content: center;
            box-shadow: 0 0 30px rgba(255,107,53,0.4), inset 0 0 20px rgba(0,0,0,0.3);
            animation: ringPulse 2s ease-in-out infinite;
            position: relative;
        }
        .logo-ring::before {
            content: ''; position: absolute;
            width: 90px; height: 90px; border-radius: 50%;
            border: 2px solid rgba(255,107,53,0.3);
            animation: ringRotate 3s linear infinite;
        }
        @keyframes ringPulse {
            0%, 100% { transform: scale(1); box-shadow: 0 0 30px rgba(255,107,53,0.4); }
            50% { transform: scale(1.05); box-shadow: 0 0 50px rgba(255,107,53,0.6); }
        }
        @keyframes ringRotate {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .logo-ring span { font-size: 36px; }
        .title {
            font-family: 'Orbitron', sans-serif;
            font-size: 22px; font-weight: 900;
            background: linear-gradient(135deg, #ff6b35, #f7931e, #ffcc00);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            background-clip: text;
            text-transform: uppercase; letter-spacing: 4px;
        }
        .subtitle {
            color: #555; font-size: 12px;
            letter-spacing: 3px; text-transform: uppercase;
            margin-top: 5px;
        }
        .tagline {
            text-align: center; color: #666;
            font-size: 13px; margin-bottom: 30px;
            line-height: 1.6;
        }
        .tagline strong { color: #ff6b35; font-weight: 600; }

        .google-btn {
            width: 100%; padding: 14px 20px;
            background: #fff; border: none;
            border-radius: 12px; color: #333;
            font-family: 'Rajdhani', sans-serif;
            font-size: 15px; font-weight: 700;
            cursor: pointer; transition: all 0.3s ease;
            display: flex; align-items: center;
            justify-content: center; gap: 12px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.3);
            position: relative; overflow: hidden;
            text-decoration: none;
        }
        .google-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(255,255,255,0.15);
        }
        .google-btn:active { transform: translateY(0); }
        .google-btn img { width: 20px; height: 20px; }

        .divider {
            display: flex; align-items: center;
            margin: 25px 0; color: #444;
            font-size: 11px; text-transform: uppercase;
            letter-spacing: 3px; font-weight: 600;
        }
        .divider::before, .divider::after {
            content: ''; flex: 1; height: 1px;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.1), transparent);
        }
        .divider span { padding: 0 20px; }

        .input-group { margin-bottom: 18px; position: relative; }
        .input-group label {
            display: block; color: #ff6b35;
            font-weight: 700; font-size: 11px;
            text-transform: uppercase; letter-spacing: 2px;
            margin-bottom: 8px; font-family: 'Orbitron', sans-serif;
        }
        .input-field {
            width: 100%; padding: 14px 16px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px; color: #fff;
            font-family: 'Rajdhani', sans-serif;
            font-size: 15px; font-weight: 500;
            transition: all 0.3s ease; outline: none;
        }
        .input-field:focus {
            border-color: #ff6b35;
            box-shadow: 0 0 0 3px rgba(255,107,53,0.1), 0 0 20px rgba(255,107,53,0.1);
            background: rgba(255,255,255,0.05);
        }
        .input-field::placeholder { color: #444; }
        .login-btn {
            width: 100%; padding: 15px;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            border: none; border-radius: 10px;
            color: #000; font-family: 'Orbitron', sans-serif;
            font-size: 13px; font-weight: 900;
            text-transform: uppercase; letter-spacing: 3px;
            cursor: pointer; transition: all 0.3s ease;
            margin-top: 5px; position: relative; overflow: hidden;
        }
        .login-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(255,107,53,0.3);
        }
        .login-btn::after {
            content: ''; position: absolute;
            top: -50%; left: -50%; width: 200%; height: 200%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2), transparent);
            transform: rotate(30deg); transition: all 0.6s;
        }
        .login-btn:hover::after { left: 100%; }
        .error-msg {
            display: none; margin-top: 15px;
            padding: 12px 15px;
            background: rgba(255,68,68,0.08);
            border: 1px solid rgba(255,68,68,0.2);
            border-radius: 10px; color: #ff4444;
            text-align: center; font-weight: 600;
            font-size: 13px; animation: shake 0.4s ease;
        }
        .error-msg.active { display: block; }
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-8px); }
            75% { transform: translateX(8px); }
        }
        .footer {
            text-align: center; margin-top: 30px;
            color: #333; font-size: 11px;
            letter-spacing: 1px;
        }
        .footer span { color: #ff6b35; font-weight: 700; }
        .footer .dev { color: #444; margin-top: 4px; font-size: 10px; }
        .security-note {
            text-align: center; margin-top: 20px;
            color: #333; font-size: 11px;
            display: flex; align-items: center;
            justify-content: center; gap: 6px;
        }
        .security-note svg { width: 14px; height: 14px; fill: #333; }
        @media (max-width: 480px) {
            .card { padding: 35px 25px; }
            .title { font-size: 18px; }
        }
    </style>
</head>
<body>
    <div class="bg-grid"></div>
    <div class="glow-orb"></div>
    <div class="glow-orb"></div>

    <div class="main-container">
        <div class="card">
            <div class="logo-section">
                <div class="logo-ring"><span>🔥</span></div>
                <h1 class="title">Free Fire</h1>
                <p class="subtitle">Like Sender Panel</p>
            </div>

            <p class="tagline">
                Welcome to <strong>SAMIUL API</strong>.<br>
                Sign in to access the like sender dashboard.
            </p>

            <a href="/google/login" class="google-btn" id="googleSignInBtn">
                <svg width="20" height="20" viewBox="0 0 24 24">
                    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/>
                    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                    <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
                    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
                </svg>
                Continue with Google
            </a>

            <div class="divider"><span>or sign in with email</span></div>

            <form id="loginForm">
                <div class="input-group">
                    <label>📧 Email Address</label>
                    <input type="email" class="input-field" id="email" placeholder="name@example.com" required>
                </div>
                <div class="input-group">
                    <label>🔒 Password</label>
                    <input type="password" class="input-field" id="password" placeholder="••••••••" required>
                </div>
                <button type="submit" class="login-btn">SIGN IN</button>
            </form>

            <div class="error-msg" id="errorMsg"></div>

            <div class="security-note">
                <svg viewBox="0 0 24 24"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z"/></svg>
                Secure & encrypted connection
            </div>
        </div>

        <div class="footer">
            <span>🔥 SAMIUL API</span>
            <div class="dev">Developed by SAMIUL</div>
        </div>
    </div>

    <script>
        document.getElementById('loginForm').addEventListener('submit', function(e) {
            e.preventDefault();
            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;
            fetch('/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({email, password})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    window.location.href = '/panel';
                } else {
                    const err = document.getElementById('errorMsg');
                    err.textContent = '❌ ' + (data.error || 'Login failed');
                    err.classList.add('active');
                }
            })
            .catch(() => {
                const err = document.getElementById('errorMsg');
                err.textContent = '❌ Network error. Please try again.';
                err.classList.add('active');
            });
        });
    </script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════
# 🌐 PROFESSIONAL DASHBOARD
# ═══════════════════════════════════════════════════════════════

PANEL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SAMIUL | Free Fire Like Sender Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Rajdhani', sans-serif;
            background: #050505;
            min-height: 100vh;
            color: #fff;
            overflow-x: hidden;
        }
        .bg-grid {
            position: fixed; top: 0; left: 0;
            width: 100%; height: 100%;
            background-image: 
                linear-gradient(rgba(255,107,53,0.02) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,107,53,0.02) 1px, transparent 1px);
            background-size: 60px 60px;
            pointer-events: none; z-index: 0;
        }
        .glow-orb {
            position: fixed; width: 500px; height: 500px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(255,107,53,0.08), transparent 70%);
            pointer-events: none; z-index: 0;
            animation: orbFloat 10s ease-in-out infinite;
        }
        .glow-orb:nth-child(1) { top: -200px; right: -200px; }
        .glow-orb:nth-child(2) { bottom: -200px; left: -200px; animation-delay: -5s; }
        @keyframes orbFloat {
            0%, 100% { transform: translate(0,0) scale(1); }
            50% { transform: translate(-20px, 20px) scale(1.1); }
        }

        .navbar {
            position: fixed; top: 0; left: 0; right: 0;
            background: rgba(10,10,10,0.9);
            backdrop-filter: blur(20px);
            border-bottom: 1px solid rgba(255,107,53,0.1);
            z-index: 100; padding: 0 30px;
            height: 65px; display: flex;
            align-items: center; justify-content: space-between;
        }
        .nav-brand {
            display: flex; align-items: center; gap: 12px;
        }
        .nav-logo {
            width: 38px; height: 38px; border-radius: 10px;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            display: flex; align-items: center; justify-content: center;
            font-size: 18px; box-shadow: 0 0 15px rgba(255,107,53,0.3);
        }
        .nav-title {
            font-family: 'Orbitron', sans-serif;
            font-size: 16px; font-weight: 900;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            letter-spacing: 2px;
        }
        .nav-tag { color: #555; font-size: 10px; letter-spacing: 2px; text-transform: uppercase; }
        .nav-right {
            display: flex; align-items: center; gap: 20px;
        }
        .nav-user {
            display: flex; align-items: center; gap: 10px;
            padding: 6px 14px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 10px;
        }
        .nav-avatar {
            width: 30px; height: 30px; border-radius: 50%;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            display: flex; align-items: center; justify-content: center;
            font-size: 14px;
        }
        .nav-name { color: #fff; font-weight: 700; font-size: 13px; }
        .nav-email { color: #555; font-size: 11px; }
        .logout-btn {
            padding: 8px 16px;
            background: rgba(255,68,68,0.1);
            border: 1px solid rgba(255,68,68,0.2);
            border-radius: 8px; color: #ff4444;
            font-family: 'Orbitron', sans-serif;
            font-size: 10px; font-weight: 700;
            text-transform: uppercase; letter-spacing: 2px;
            cursor: pointer; text-decoration: none;
            transition: all 0.3s;
        }
        .logout-btn:hover {
            background: rgba(255,68,68,0.2);
            box-shadow: 0 0 15px rgba(255,68,68,0.2);
        }

        .main-content {
            padding: 100px 20px 40px;
            max-width: 900px; margin: 0 auto;
            position: relative; z-index: 1;
        }
        .page-header {
            text-align: center; margin-bottom: 40px;
        }
        .page-header h1 {
            font-family: 'Orbitron', sans-serif;
            font-size: 28px; font-weight: 900;
            background: linear-gradient(135deg, #ff6b35, #f7931e, #ffcc00);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            letter-spacing: 3px; margin-bottom: 8px;
        }
        .page-header p { color: #555; font-size: 14px; letter-spacing: 1px; }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px; margin-bottom: 30px;
        }
        .stat-card {
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.05);
            border-radius: 16px;
            padding: 20px;
            position: relative; overflow: hidden;
            transition: all 0.3s;
        }
        .stat-card:hover {
            border-color: rgba(255,107,53,0.2);
            transform: translateY(-2px);
        }
        .stat-card::before {
            content: ''; position: absolute; top: 0; left: 0;
            width: 3px; height: 100%;
            background: linear-gradient(180deg, #ff6b35, #f7931e);
        }
        .stat-label {
            color: #555; font-size: 11px;
            text-transform: uppercase; letter-spacing: 2px;
            font-weight: 700; margin-bottom: 8px;
        }
        .stat-value {
            font-family: 'Orbitron', sans-serif;
            font-size: 24px; font-weight: 900;
            color: #fff;
        }
        .stat-value.accent {
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }

        .main-card {
            background: rgba(15,15,15,0.98);
            border: 1px solid rgba(255,107,53,0.12);
            border-radius: 24px;
            padding: 35px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.6);
            position: relative; overflow: hidden;
        }
        .main-card::after {
            content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
            background: linear-gradient(90deg, transparent, #ff6b35, #f7931e, transparent);
        }
        .card-title {
            font-family: 'Orbitron', sans-serif;
            font-size: 14px; color: #ff6b35;
            text-transform: uppercase; letter-spacing: 3px;
            margin-bottom: 25px; display: flex;
            align-items: center; gap: 10px;
        }
        .card-title::before {
            content: ''; width: 4px; height: 20px;
            background: linear-gradient(180deg, #ff6b35, #f7931e);
            border-radius: 2px;
        }

        .form-row {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 15px; margin-bottom: 20px;
        }
        @media (max-width: 600px) {
            .form-row { grid-template-columns: 1fr; }
        }
        .input-group { position: relative; }
        .input-group label {
            display: block; color: #ff6b35;
            font-weight: 700; font-size: 10px;
            text-transform: uppercase; letter-spacing: 2px;
            margin-bottom: 8px; font-family: 'Orbitron', sans-serif;
        }
        .input-field {
            width: 100%; padding: 14px 16px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px; color: #fff;
            font-family: 'Rajdhani', sans-serif;
            font-size: 15px; font-weight: 500;
            transition: all 0.3s ease; outline: none;
        }
        .input-field:focus {
            border-color: #ff6b35;
            box-shadow: 0 0 0 3px rgba(255,107,53,0.1), 0 0 20px rgba(255,107,53,0.1);
            background: rgba(255,255,255,0.05);
        }
        .input-field::placeholder { color: #333; }
        select.input-field {
            cursor: pointer; appearance: none;
            background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='%23ff6b35' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
            background-repeat: no-repeat;
            background-position: right 12px center;
            background-size: 18px;
        }

        .send-btn {
            width: 100%; padding: 16px;
            background: linear-gradient(135deg, #ff6b35, #f7931e);
            border: none; border-radius: 12px;
            color: #000; font-family: 'Orbitron', sans-serif;
            font-size: 14px; font-weight: 900;
            text-transform: uppercase; letter-spacing: 3px;
            cursor: pointer; transition: all 0.3s ease;
            position: relative; overflow: hidden;
            margin-top: 5px;
        }
        .send-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 15px 40px rgba(255,107,53,0.3);
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

        .loading {
            display: none; text-align: center;
            margin-top: 25px;
        }
        .loading.active { display: block; }
        .spinner {
            width: 40px; height: 40px;
            border: 3px solid rgba(255,107,53,0.15);
            border-top-color: #ff6b35;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 12px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading-text {
            color: #ff6b35; font-family: 'Orbitron', sans-serif;
            font-size: 12px; letter-spacing: 2px;
        }

        .result-card {
            display: none; margin-top: 25px;
            background: rgba(255,107,53,0.04);
            border: 1px solid rgba(255,107,53,0.15);
            border-radius: 16px; padding: 20px;
            animation: slideUp 0.5s ease;
        }
        .result-card.active { display: block; }
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .result-header {
            display: flex; align-items: center; gap: 10px;
            margin-bottom: 15px; padding-bottom: 12px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }
        .result-header h3 {
            font-family: 'Orbitron', sans-serif;
            color: #ff6b35; font-size: 12px;
            text-transform: uppercase; letter-spacing: 2px;
        }
        .result-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 10px;
        }
        .result-item {
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.04);
            border-radius: 10px; padding: 12px 15px;
        }
        .result-label { color: #555; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px; }
        .result-value {
            color: #fff; font-weight: 700;
            font-size: 15px; font-family: 'Orbitron', sans-serif;
        }
        .result-value.success { color: #00ff88; }
        .result-value.error { color: #ff4444; }
        .result-value.warning { color: #ffcc00; }
        .status-badge {
            display: inline-block; padding: 3px 10px;
            border-radius: 20px; font-size: 10px;
            font-weight: 700; text-transform: uppercase;
        }
        .status-success { background: rgba(0,255,136,0.1); color: #00ff88; border: 1px solid rgba(0,255,136,0.2); }
        .status-failed { background: rgba(255,68,68,0.1); color: #ff4444; border: 1px solid rgba(255,68,68,0.2); }
        .status-pending { background: rgba(255,204,0,0.1); color: #ffcc00; border: 1px solid rgba(255,204,0,0.2); }

        .error-msg {
            display: none; margin-top: 18px;
            padding: 14px 16px;
            background: rgba(255,68,68,0.06);
            border: 1px solid rgba(255,68,68,0.15);
            border-radius: 12px; color: #ff4444;
            text-align: center; font-weight: 600;
            font-size: 13px; animation: shake 0.4s ease;
        }
        .error-msg.active { display: block; }
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-8px); }
            75% { transform: translateX(8px); }
        }

        .footer {
            text-align: center; margin-top: 40px;
            color: #222; font-size: 11px;
            letter-spacing: 1px; padding-bottom: 20px;
        }
        .footer span { color: #ff6b35; font-weight: 700; }
        .footer .dev { color: #333; margin-top: 4px; font-size: 10px; }
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
        <div class="nav-right">
            <div class="nav-user">
                <div class="nav-avatar">👤</div>
                <div>
                    <div class="nav-name">{{ user_name }}</div>
                    <div class="nav-email">{{ user_email }}</div>
                </div>
            </div>
            <a href="/logout" class="logout-btn">Logout</a>
        </div>
    </nav>

    <div class="main-content">
        <div class="page-header">
            <h1>Like Sender Dashboard</h1>
            <p>Enter player UID and region to send likes instantly</p>
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
                    <input type="text" class="input-field" id="uid" placeholder="Enter Free Fire UID" maxlength="20">
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

            <button class="send-btn" id="sendBtn">
                ⚡ SEND LIKES
            </button>

            <div class="loading" id="loading">
                <div class="spinner"></div>
                <p class="loading-text">PROCESSING REQUEST...</p>
            </div>

            <div class="error-msg" id="errorMsg"></div>

            <div class="result-card" id="resultCard">
                <div class="result-header">
                    <h3>📊 Player Information</h3>
                </div>
                <div class="result-grid" id="resultContent"></div>
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
            const btn = document.getElementById('sendBtn');
            btn.disabled = false;
            btn.textContent = '⚡ SEND LIKES';
        }

        function showError(msg) {
            const el = document.getElementById('errorMsg');
            el.textContent = msg;
            el.classList.add('active');
            setTimeout(() => el.classList.remove('active'), 10000);
        }

        function updateStats(used, limit, remaining) {
            document.getElementById('statUsed').textContent = used || 0;
            document.getElementById('statRemaining').textContent = remaining !== undefined ? remaining : (limit - (used||0));
            document.getElementById('statLimit').textContent = limit;
        }

        function displayResult(data) {
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
                    <div class="result-label">Status</div>
                    <div><span class="status-badge status-${sc}">${st}</span></div>
                </div>
            `;
            document.getElementById('resultCard').classList.add('active');
        }

        async function sendLike() {
            const uid = document.getElementById('uid').value.trim();
            const region = document.getElementById('region').value;
            const btn = document.getElementById('sendBtn');
            const loading = document.getElementById('loading');

            resetUI();

            if (!uid) { showError('❌ Please enter a valid UID!'); return; }
            if (!/^\d+$/.test(uid)) { showError('❌ UID must contain only numbers!'); return; }

            btn.disabled = true;
            btn.textContent = '⏳ PROCESSING...';
            loading.classList.add('active');

            try {
                const apiUrl = `/like?key=${API_KEY}&uid=${encodeURIComponent(uid)}&server_name=${encodeURIComponent(region)}`;
                const resp = await fetch(apiUrl);
                const data = await resp.json();

                resetUI();

                if (data.error) {
                    showError(`❌ ${data.error}`);
                    return;
                }

                displayResult(data);
                // Parse remains string like "(85/90)"
                const remainsMatch = data.remains ? data.remains.match(/\d+/) : null;
                const used = remainsMatch ? (90 - parseInt(remainsMatch[0])) : 0;
                updateStats(used, 90, remainsMatch ? parseInt(remainsMatch[0]) : 90);

            } catch (e) {
                console.error(e);
                resetUI();
                showError('❌ Network error! Please try again.');
            }
        }

        document.getElementById('sendBtn').addEventListener('click', sendLike);
        document.getElementById('uid').addEventListener('keypress', e => {
            if (e.key === 'Enter') sendLike();
        });
    </script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def login_page():
    if 'user' in session:
        return redirect('/panel')
    return render_template_string(LOGIN_TEMPLATE)


@app.route('/google/login')
def google_login():
    """Redirect to Google OAuth - NO POPUP, so NO BLOCK"""
    if GOOGLE_CLIENT_ID.startswith("YOUR_GOOGLE"):
        return """
        <html><body style="background:#050505;color:#ff6b35;font-family:Orbitron;text-align:center;padding-top:100px;">
        <h1>⚠️ Google Client ID Not Configured</h1>
        <p style="color:#888;">Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the code.</p>
        <a href="/" style="color:#f7931e;">← Go Back</a>
        </body></html>
        """, 400

    import secrets
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state

    scope = "openid email profile"
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        "&response_type=code"
        f"&scope={scope}"
        f"&state={state}"
        "&prompt=select_account"
        "&access_type=offline"
    )
    return redirect(auth_url)


@app.route('/google/callback')
def google_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    stored_state = session.get('oauth_state')

    if not code:
        return redirect('/')

    if stored_state and state != stored_state:
        return "❌ Invalid state parameter. Possible CSRF attack.", 403

    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code"
    }

    try:
        resp = requests.post(token_url, data=token_data, timeout=10)
        token_info = resp.json()

        if "error" in token_info:
            return f"❌ Google OAuth Error: {token_info.get('error_description', token_info['error'])}", 400

        access_token = token_info.get("access_token")
        user_info_resp = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        user_info = user_info_resp.json()

        session['user'] = {
            'name': user_info.get('name', 'User'),
            'email': user_info.get('email', ''),
            'picture': user_info.get('picture', ''),
            'logged_in': True
        }
        session.pop('oauth_state', None)
        return redirect('/panel')

    except Exception as e:
        return f"❌ Error during Google login: {str(e)}", 500


@app.route('/login', methods=['POST'])
def manual_login():
    data = request.get_json()
    email = data.get('email', '')
    password = data.get('password', '')

    if email and password:
        session['user'] = {
            'name': email.split('@')[0],
            'email': email,
            'logged_in': True
        }
        return jsonify({"success": True})

    return jsonify({"success": False, "error": "Invalid credentials"})


@app.route('/panel')
@login_required
def panel():
    user = session.get('user', {})
    return render_template_string(
        PANEL_TEMPLATE,
        user_name=user.get('name', 'User'),
        user_email=user.get('email', '')
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')


# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS (PROTECTED)
# ═══════════════════════════════════════════════════════════════

@app.route('/like', methods=['GET'])
@login_required
def handle_requests():
    uid = request.args.get("uid")
    server_name = request.args.get("server_name", "").upper()
    key = request.args.get("key")
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
    except:
        return jsonify({"error": "Data parsing failed", "status": 0}), 200

    if server_name == "IND":
        like_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        like_url = "https://client.us.freefiremobile.com/LikeProfile"
    else:
        like_url = "https://clientbp.ggpolarbear.com/LikeProfile"

    result = asyncio.run(send_all_likes(uid, server_name, like_url))

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

        return jsonify({
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": after_like,
            "LikesbeforeCommand": before_like,
            "PlayerNickname": player_name,
            "UID": player_id,
            "status": status,
            "remains": f"({remains}/{KEY_LIMIT})",
        })
    except Exception as e:
        return jsonify({"error": str(e), "status": 0}), 500


@app.route('/reset-cache', methods=['GET'])
@login_required
def reset_cache():
    key = request.args.get("key")
    if key != "JMLB":
        return jsonify({"error": "Invalid key"}), 403

    global liked_cache
    liked_cache.clear()
    return jsonify({"message": "Cache cleared", "credit": "Samiul"})


if __name__ == '__main__':
    print("🚀 SAMIUL Free Fire Like Sender Server Started!")
    print("📁 Required account files:")
    print("   - account_ind.txt (IND server)")
    print("   - account_br.txt (BR/US/SAC/NA servers)")
    print("   - account_bd.txt (BD/RU server)")
    print("🔐 Login required for all access")
    print("🌐 Open http://localhost:5000 in your browser")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
