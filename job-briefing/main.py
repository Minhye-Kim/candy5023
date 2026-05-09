import os
import re
import json
import base64
import requests
from datetime import datetime
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from groq import Groq


# ── Gmail 인증 ──────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ['GMAIL_REFRESH_TOKEN'],
        client_id=os.environ['GMAIL_CLIENT_ID'],
        client_secret=os.environ['GMAIL_CLIENT_SECRET'],
        token_uri='https://oauth2.googleapis.com/token',
        scopes=['https://www.googleapis.com/auth/gmail.readonly']
    )
    creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)


# ── 이메일 수집 ─────────────────────────────────────────────────────────────

def search_emails(service, query, max_results=50):
    try:
        results = service.users().messages().list(
            userId='me', q=query, maxResults=max_results
        ).execute()
        return results.get('messages', [])
    except Exception as e:
        print(f"[검색 오류] {e}")
        return []


def decode_body(data: str) -> str:
    if not data:
        return ''
    try:
        return base64.urlsafe_b64decode(data + '==').decode('utf-8', errors='ignore')
    except Exception:
        return ''


def extract_html(payload: dict) -> str:
    mime = payload.get('mimeType', '')
    if mime == 'text/html':
        return decode_body(payload.get('body', {}).get('data', ''))
    if 'parts' in payload:
        return ' '.join(extract_html(p) for p in payload['parts'])
    return ''


def parse_linkedin_jobs(raw_html: str) -> list:
    jobs, seen = [], set()
    blocks = re.findall(
        r'<a\b[^>]*href="(https://www\.linkedin\.com/(?:comm/)?jobs/view/(\d+)[^"]*)"[^>]*>(.*?)</a>',
        raw_html,
        re.DOTALL | re.IGNORECASE,
    )
    for _, job_id, inner in blocks:
        title = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', inner)).strip()
        if job_id in seen or len(title) < 5:
            continue
        seen.add(job_id)
        jobs.append({'title': title, 'url': f'https://www.linkedin.com/jobs/view/{job_id}'})
    return jobs


def parse_jobkorea_jobs(raw_html: str) -> list:
    jobs, seen = [], set()
    blocks = re.findall(
        r'<a\b[^>]*href="(https://www\.jobkorea\.co\.kr/Recruit/GI_Read/(\d+)[^"]*)"[^>]*>(.*?)</a>',
        raw_html,
        re.DOTALL | re.IGNORECASE,
    )
    for _, job_id, inner in blocks:
        title = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', inner)).strip()
        if job_id in seen or len(title) < 5:
            continue
        seen.add(job_id)
        jobs.append({'title': title, 'url': f'https://www.jobkorea.co.kr/Recruit/GI_Read/{job_id}'})
    return jobs


def extract_text(payload: dict) -> str:
    mime = payload.get('mimeType', '')
    if mime == 'text/plain':
        return decode_body(payload.get('body', {}).get('data', ''))
    if mime == 'text/html':
        html = decode_body(payload.get('body', {}).get('data', ''))
        text = re.sub(r'<[^>]+>', ' ', html)
        return re.sub(r'\s+', ' ', text).strip()
    if 'parts' in payload:
        return ' '.join(extract_text(p) for p in payload['parts'])
    return ''


def get_email_detail(service, msg_id: str) -> Optional[dict]:
    try:
        msg = service.users().messages().get(
            userId='me', id=msg_id, format='full'
        ).execute()

        headers = msg['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
        sender  = next((h['value'] for h in headers if h['name'] == 'From'), '')
        snippet = msg.get('snippet', '')

        body = extract_text(msg['payload'])
        raw_html = extract_html(msg['payload'])
        linkedin_urls = re.findall(r'https://www\.linkedin\.com/(?:comm/)?jobs/view/\d+', body)
        jobkorea_urls = list(dict.fromkeys(
            re.findall(r'https://www\.jobkorea\.co\.kr/Recruit/GI_Read/\d+', raw_html)
        ))
        jobkorea_jobs = parse_jobkorea_jobs(raw_html) if 'jobkorea.co.kr' in sender.lower() else []
        linkedin_jobs = parse_linkedin_jobs(raw_html) if 'linkedin' in sender.lower() else []

        return {
        'subject': subject[:150],
        'sender': sender[:80],
        'snippet': snippet[:200],
        'body': body[:300],
        'linkedin_urls': linkedin_urls[:3],
        'jobkorea_urls': jobkorea_urls[:3],
        'linkedin_jobs': linkedin_jobs,
        'jobkorea_jobs': jobkorea_jobs,
        }

    except Exception as e:
        print(f"[메일 읽기 오류] {msg_id}: {e}")
        return None


# ── Groq 요약 ───────────────────────────────────────────────────────────────

def summarize_with_groq(emails: list) -> str:
    client = Groq(api_key=os.environ['GROQ_API_KEY'])
    today  = datetime.now().strftime('%Y-%m-%d')

    prompt = f"""오늘 날짜: {today}
아래는 최근 7일간 받은 채용 관련 이메일 목록입니다.

{json.dumps(emails, ensure_ascii=False, indent=2)}

다음 규칙에 따라 IT 직군 채용 공고만 정리해주세요.

포함 직군: PM, PO, Product Manager, Product Owner, AI Engineer, ML Engineer, 서비스 기획, 기획자, 데이터 분석, UX, 백엔드, 프론트엔드, 풀스택, DevOps, MLOps, LLM, AI

제외: 인턴, 알바, 단기, 게임회사(넥슨, 크래프톤, NC소프트, 넷마블, 스마일게이트, 펄어비스, 컴투스, Riot Games)

출력 형식 (공고 1개당):
---
🏢 **[회사명]** — [포지션]
📍 [근무지] | ⏰ [마감일] ← 정보 없으면 이 줄 생략
• [핵심 내용 1~2줄] ← 정보 없으면 이 줄 생략
🔗 [지원 링크]

링크 규칙: linkedin_jobs[].url, jobkorea_jobs[].url, linkedin_urls, jobkorea_urls 순으로 사용. 없으면 🔗 줄 생략. 절대 추측 금지.
데이터 규칙: linkedin_jobs 또는 jobkorea_jobs 배열이 있으면 각 항목을 개별 공고로 처리. 공고명은 title 필드 사용.

마지막 줄: 📬 총 N개 공고 | PM/PO N개 · AI N개 · 개발 N개 · 기타 N개

공고가 없으면 "📭 해당 기간 내 IT 직군 채용 공고 없음"만 출력."""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000
    )
    return response.choices[0].message.content


# ── Discord 전송 ────────────────────────────────────────────────────────────

def send_to_discord(content: str):
    webhook_url = os.environ['DISCORD_WEBHOOK_URL']
    today = datetime.now().strftime('%Y년 %m월 %d일')

    # 헤더 메시지
    requests.post(webhook_url, json={
        "embeds": [{
            "title": f"📬 채용 공고 브리핑 — {today}",
            "color": 5814783
        }]
    })

    # 본문을 1900자 단위로 분할 (Discord 2000자 제한)
    chunks, current = [], ''
    for line in content.split('\n'):
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = line
        else:
            current += ('\n' if current else '') + line
    if current:
        chunks.append(current)

    for chunk in chunks:
        resp = requests.post(webhook_url, json={"content": chunk})
        if resp.status_code not in (200, 204):
            print(f"[Discord 오류] {resp.status_code}: {resp.text}")


# ── 메인 ────────────────────────────────────────────────────────────────────

def main():
    print("▶ Gmail 연결 중...")
    service = get_gmail_service()

    query_a = (
        "from:(linkedin OR wanted OR jobkorea OR saramin OR jumpit"
        " OR rocketpunch OR programmers OR grepp) newer_than:7d"
    )
    query_b = (
        'subject:(채용 OR 공고 OR "JD" OR 포지션 OR "job opening"'
        ' OR "we\'re hiring" OR 리쿠르팅) newer_than:7d'
    )
    query_c = "from:smartai@jobkorea.co.kr newer_than:7d"

    print("▶ 이메일 검색 중...")
    msgs_a = search_emails(service, query_a)
    msgs_b = search_emails(service, query_b)
    msgs_c = search_emails(service, query_c)
    all_ids = list({m['id'] for m in msgs_a + msgs_b + msgs_c})
    print(f"  총 {len(all_ids)}개 메일 발견")

    if not all_ids:
        send_to_discord("📭 최근 7일간 채용 관련 메일이 없습니다.")
        return

    print("▶ 메일 내용 읽는 중...")
    emails = [d for mid in all_ids[:30] if (d := get_email_detail(service, mid))]
    print(f"  {len(emails)}개 메일 수집 완료")

    print("▶ Grok으로 요약 중...")
    summary = summarize_with_groq(emails)

    print("▶ Discord 전송 중...")
    send_to_discord(summary)
    print("✅ 완료!")


if __name__ == '__main__':
    main()
