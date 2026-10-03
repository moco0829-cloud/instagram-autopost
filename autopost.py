"""Instagram 自動投稿スクリプト。

使い方:
    python autopost.py generate [--slot morning|noon|night]   # 文章と画像を作って posts/ に保存
    python autopost.py publish                                # posts/latest.json の投稿を Instagram に公開
    python autopost.py refresh-token                          # 長期アクセストークンを延長して標準出力に出す

generate と publish を分けているのは、Instagram API が「インターネット上で公開されている画像URL」
しか受け付けないためです。GitHub Actions では generate → git push → publish の順に実行し、
push した画像の raw.githubusercontent.com の URL を Instagram に渡します。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests
from openai import OpenAI
from PIL import Image

ROOT = Path(__file__).resolve().parent
POSTS_DIR = ROOT / "posts"
HISTORY_FILE = ROOT / "history.json"
LATEST_FILE = POSTS_DIR / "latest.json"
BRAND_FILE = ROOT / "brand.md"

JST = timezone(timedelta(hours=9), "JST")
IG_API = "https://graph.instagram.com/v23.0"

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-1")
IMAGE_QUALITY = os.environ.get("IMAGE_QUALITY", "medium")

SLOTS = {
    "morning": "朝（7時ごろ）。通勤・始業前に読まれる。前向きで軽く、1日のスタートに役立つ内容。",
    "noon": "昼（12時ごろ）。昼休みにさっと読まれる。すぐ試せる具体的なTipsや小ネタ。",
    "night": "夜（19時ごろ）。仕事終わりにゆっくり読まれる。少し深い考察、事例、振り返りにつながる内容。",
}

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "theme": {"type": "string", "description": "投稿テーマを20字以内で。履歴との重複チェックに使う"},
        "caption": {"type": "string", "description": "Instagramのキャプション本文。末尾にハッシュタグを含める"},
        "image_prompt": {"type": "string", "description": "画像生成AIに渡す英語のプロンプト"},
    },
    "required": ["theme", "caption", "image_prompt"],
    "additionalProperties": False,
}


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"環境変数 {name} が設定されていません")
    return value


def detect_slot(now: datetime) -> str:
    if now.hour < 10:
        return "morning"
    if now.hour < 16:
        return "noon"
    return "night"


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return []


# ---------------------------------------------------------------- generate

def write_post(slot: str, now: datetime, history: list[dict]) -> dict:
    """Claude にテーマ・キャプション・画像プロンプトを考えてもらう。"""
    brand = BRAND_FILE.read_text(encoding="utf-8")
    recent = "\n".join(f"- {h['date']} {h['theme']}" for h in history[-45:]) or "（まだありません）"

    system = (
        "あなたはInstagram運用の担当者です。以下のアカウント設定に沿って、1件分の投稿を作ります。\n\n"
        f"{brand}\n\n"
        "## 出力のルール\n"
        "- caption: 日本語。1行目は思わず続きを読みたくなるフック。本文は改行と空行で読みやすく、"
        "全体で300〜600字。最後に関連ハッシュタグを8〜15個。\n"
        "- image_prompt: 英語。投稿内容を象徴する1枚の画像を説明する。"
        "画像生成AIは日本語の文字をうまく描けないので、画像内に文字・ロゴ・数字を入れないよう"
        "明記すること（例: 'no text, no letters, no logos'）。縦長構図で、主題は中央付近に置く。\n"
        "- 過去の投稿テーマと内容が重ならないようにする。"
    )
    user = (
        f"今日は {now:%Y年%m月%d日（%a）} です。\n"
        f"投稿枠: {SLOTS[slot]}\n\n"
        f"過去の投稿テーマ:\n{recent}"
    )

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": POST_SCHEMA}},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "refusal":
        sys.exit(f"Claude が生成を断りました: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        sys.exit("Claude の出力が途中で切れました")

    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def make_image(prompt: str, dest: Path) -> None:
    """OpenAI で画像を作り、Instagram フィード向けの 1080x1350（4:5）JPEG にする。"""
    client = OpenAI()
    result = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1536",
        quality=IMAGE_QUALITY,
        n=1,
    )
    image = Image.open(io.BytesIO(base64.b64decode(result.data[0].b64_json))).convert("RGB")

    # 2:3 の画像を中央で 4:5 に切り抜く（Instagram の縦長上限が 4:5 のため）
    width, height = image.size
    target_height = width * 5 // 4
    top = (height - target_height) // 2
    image = image.crop((0, top, width, top + target_height)).resize((1080, 1350), Image.LANCZOS)
    image.save(dest, "JPEG", quality=92)


def cmd_generate(slot: str | None) -> None:
    now = datetime.now(JST)
    slot = slot or detect_slot(now)
    history = load_history()

    post = write_post(slot, now, history)
    print(f"テーマ: {post['theme']}\n\n{post['caption']}\n")

    POSTS_DIR.mkdir(exist_ok=True)
    image_name = f"{now:%Y%m%d}-{slot}.jpg"
    make_image(post["image_prompt"], POSTS_DIR / image_name)

    latest = {
        "date": now.strftime("%Y-%m-%d"),
        "slot": slot,
        "theme": post["theme"],
        "caption": post["caption"],
        "image_prompt": post["image_prompt"],
        "image": f"posts/{image_name}",
        "published": False,
    }
    LATEST_FILE.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"保存しました: {latest['image']}")


# ---------------------------------------------------------------- publish

def ig_request(method: str, path: str, **params) -> dict:
    params["access_token"] = env("IG_ACCESS_TOKEN")
    response = requests.request(method, f"{IG_API}/{path}", params=params, timeout=60)
    if not response.ok:
        sys.exit(f"Instagram API エラー ({response.status_code}): {response.text}")
    return response.json()


def wait_until_ready(container_id: str) -> None:
    for _ in range(30):
        status = ig_request("GET", container_id, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            sys.exit(f"Instagram 側で画像の処理に失敗しました: {status}")
        time.sleep(5)
    sys.exit("Instagram 側の画像処理が時間内に終わりませんでした")


def cmd_publish() -> None:
    latest = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
    if latest["published"]:
        sys.exit("この投稿はすでに公開済みです")

    # GitHub Actions が push した直後のコミットを指す URL。コミットSHAで固定するので内容が確実に一致する。
    repo = env("GITHUB_REPOSITORY")
    sha = env("IMAGE_COMMIT_SHA")
    image_url = f"https://raw.githubusercontent.com/{repo}/{sha}/{latest['image']}"

    user_id = env("IG_USER_ID")
    container = ig_request("POST", f"{user_id}/media", image_url=image_url, caption=latest["caption"])
    wait_until_ready(container["id"])
    media = ig_request("POST", f"{user_id}/media_publish", creation_id=container["id"])
    print(f"投稿しました: media_id={media['id']}")

    latest["published"] = True
    latest["media_id"] = media["id"]
    LATEST_FILE.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    history = load_history()
    history.append({k: latest[k] for k in ("date", "slot", "theme", "image", "media_id")})
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_refresh_token() -> None:
    response = requests.get(
        "https://graph.instagram.com/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": env("IG_ACCESS_TOKEN")},
        timeout=60,
    )
    if not response.ok:
        sys.exit(f"トークンの延長に失敗しました ({response.status_code}): {response.text}")
    print(response.json()["access_token"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--slot", choices=SLOTS.keys())
    sub.add_parser("publish")
    sub.add_parser("refresh-token")
    args = parser.parse_args()

    if args.command == "generate":
        cmd_generate(args.slot)
    elif args.command == "publish":
        cmd_publish()
    else:
        cmd_refresh_token()


if __name__ == "__main__":
    main()
