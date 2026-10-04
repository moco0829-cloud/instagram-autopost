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
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

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
SIGNATURE = os.environ.get("IMAGE_SIGNATURE", "Salon de moco")
SIGNATURE_SUB = os.environ.get("IMAGE_SIGNATURE_SUB", "FemCare by Mediser")

# 画像に重ねる文字の色（今のフィードに合わせた、こげ茶・ローズピンク・ゴールド）
COLOR_TEXT = (92, 62, 54)
COLOR_ACCENT = (214, 106, 138)
COLOR_GOLD = (201, 164, 98)
COLOR_PANEL = (255, 249, 246, 215)

# 日本語の明朝体。上から順に見つかったものを使う（GitHub Actions では fonts-noto-cjk を入れる）
FONT_CANDIDATES = [
    os.environ.get("FONT_PATH", ""),
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "C:/Windows/Fonts/NotoSerifJP-VF.ttf",
    "C:/Windows/Fonts/yumindb.ttf",
]

SLOTS = {
    "morning": "朝（7時ごろ）。1日の始まりに読まれる。前向きで軽く、今日から意識できる小さな習慣。",
    "noon": "昼（12時ごろ）。休憩中にさっと読まれる。すぐ試せる具体的なセルフケアや豆知識。",
    "night": "夜（19時ごろ）。1日の終わりにゆっくり読まれる。悩みに深く寄り添う内容や、自分をいたわる時間の提案。",
}

TEXT_PAIR = {
    "type": "object",
    "properties": {
        "lead": {"type": "string", "description": "画像の上部に小さく入れる一言。15字以内"},
        "headline": {"type": "string", "description": "画像の中央に大きく入れる見出し。1行9字以内で2〜3行、改行は\\n。ピンクで強調する言葉は【】で囲む"},
    },
    "required": ["lead", "headline"],
    "additionalProperties": False,
}

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "theme": {"type": "string", "description": "投稿テーマを20字以内で。履歴との重複チェックに使う"},
        "caption": {"type": "string", "description": "Instagramのキャプション本文。末尾にハッシュタグを含める"},
        "cover": {**TEXT_PAIR, "description": "1枚目（表紙）の文字"},
        "slides": {
            "type": "array",
            "description": "2枚目以降の中身の画像。1〜3枚",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "画像の見出し。1行12字以内で1〜2行、改行は\\n。強調は【】"},
                    "numbered": {"type": "boolean", "description": "手順など順番のある内容なら true（番号付き）、チェックリストやポイントなら false"},
                    "items": {"type": "array", "items": {"type": "string"}, "description": "箇条書きの中身。3〜5個、各40字以内"},
                },
                "required": ["title", "numbered", "items"],
                "additionalProperties": False,
            },
        },
        "closing": {**TEXT_PAIR, "description": "最後の画像の文字。DMでの相談をやさしく促す"},
        "image_prompt": {"type": "string", "description": "画像生成AIに渡す英語のプロンプト"},
    },
    "required": ["theme", "caption", "cover", "slides", "closing", "image_prompt"],
    "additionalProperties": False,
}


def env(name: str) -> str:
    # Secrets に貼り付けたときの前後の空白・改行は取り除く
    value = os.environ.get(name, "").strip()
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
        "- 投稿は、横にスワイプして見るカルーセル形式。1枚目が表紙（cover）、2枚目以降が中身（slides）、"
        "最後が締め（closing）。読んだ人が保存したくなる、役立つ内容を画像にまとめる。\n"
        "- caption: 日本語。1行目は思わず続きを読みたくなるフック。本文は改行と空行で読みやすく、"
        "全体で300〜500字。画像の内容をなぞりつつ、共感や補足を加え、"
        "「画像をスワイプしてご覧くださいね」のように画像へ誘導する。最後に関連ハッシュタグを8〜15個。\n"
        "- cover の lead と headline: フィードで目に留まり、続きを見たくなる言葉にする。"
        "headline は問いかけや共感の一言が効果的。いちばん伝えたい言葉を1〜2か所だけ【】で囲むと、"
        "画像ではそこがピンクの少し大きな文字になる（例: 'その【生理痛】、\\nがまんして\\nいませんか？'）。"
        "改行位置は意味の切れ目にする。\n"
        "- slides: 1〜3枚。1枚に1つのまとまり（手順、チェックリスト、ポイントなど）。"
        "items は画像に入れるので、短く言い切る（各40字以内、3〜5個）。\n"
        "- closing: サロンの想いを感じる、やさしい締め。DMでの相談をさりげなく促す"
        "（例: lead 'ひとりで抱えないで' headline 'お気軽に\\n【ご相談】\\nくださいね'）。\n"
        "- image_prompt: 英語。投稿内容を象徴する背景画像を説明する。"
        "雰囲気は、クリーム色〜淡いピンクの明るい背景に、ピンクの芍薬やバラの花を水彩画風にあしらい、"
        "細い金色のラインやきらめきを添えた、上品でフェミニンな大人の女性向けのデザイン。"
        "あとから中央に日本語の見出しを重ねるので、中央は明るく余白の多い淡い色にし、"
        "花や人物などのモチーフは四隅や左右の端に寄せる。"
        "画像生成AIは日本語の文字をうまく描けないので、画像内に文字・ロゴ・数字を入れないよう"
        "明記すること（例: 'no text, no letters, no logos'）。\n"
        "- 過去の投稿テーマと内容が重ならないようにする。"
    )
    user = (
        f"今日は {now:%Y年%m月%d日（%a）} です。\n"
        f"投稿枠: {SLOTS[slot]}\n\n"
        f"過去の投稿テーマ:\n{recent}"
    )

    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
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


def make_background(prompt: str) -> Image.Image:
    """OpenAI で背景画像を作り、Instagram フィード向けの 1080x1350（4:5）にする。"""
    client = OpenAI(api_key=env("OPENAI_API_KEY"))
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
    return image.crop((0, top, width, top + target_height)).resize((1080, 1350), Image.LANCZOS)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in filter(None, FONT_CANDIDATES):
        if not Path(path).exists():
            continue
        if path.endswith(".ttc"):
            # CJK フォント集の中から日本語（JP）版を探す
            for index in range(10):
                try:
                    font = ImageFont.truetype(path, size, index=index)
                except OSError:
                    break
                if "JP" in font.getname()[0]:
                    return font
            continue
        font = ImageFont.truetype(path, size)
        try:
            font.set_variation_by_axes([700])  # 可変フォントなら太字に
        except OSError:
            pass
        return font
    sys.exit("日本語フォントが見つかりません。環境変数 FONT_PATH で明朝体フォントを指定してください")


def parse_emphasis(line: str) -> list[tuple[str, bool]]:
    """'その【生理痛】、' → [('その', False), ('生理痛', True), ('、', False)]"""
    return [(part, i % 2 == 1) for i, part in enumerate(re.split(r"[【】]", line)) if part]


def draw_line(draw: ImageDraw.ImageDraw, baseline: int, segments, fonts) -> None:
    """強調部分だけピンク・大きめにして、1行を中央揃えで描く（ベースラインをそろえる）。"""
    width = sum(fonts[emph].getlength(text) for text, emph in segments)
    x = (1080 - width) / 2
    for text, emph in segments:
        draw.text((x, baseline), text, font=fonts[emph], fill=COLOR_ACCENT if emph else COLOR_TEXT, anchor="ls")
        x += fonts[emph].getlength(text)


def draw_heart(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    draw.ellipse((cx - r, cy - r * 0.9, cx, cy + r * 0.1), fill=fill)
    draw.ellipse((cx, cy - r * 0.9, cx + r, cy + r * 0.1), fill=fill)
    draw.polygon([(cx - r * 0.97, cy - r * 0.25), (cx + r * 0.97, cy - r * 0.25), (cx, cy + r * 1.05)], fill=fill)


def draw_sparkle(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    w = r * 0.28
    draw.polygon([(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w),
                  (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)], fill=fill)


def split_lines(text: str) -> list[list[tuple[str, bool]]]:
    return [parse_emphasis(line.strip()) for line in text.replace("\\n", "\n").splitlines() if line.strip()]


def fit_heading(lines, max_size: int, min_size: int) -> tuple[int, dict]:
    """一番長い行が幅 820px に収まるまで文字を小さくする（強調部分は 1.15 倍）。"""
    size = max_size
    while True:
        fonts = {False: load_font(size), True: load_font(int(size * 1.15))}
        widest = max(sum(fonts[e].getlength(t) for t, e in line) for line in lines)
        if widest <= 820 or size <= min_size:
            return size, fonts
        size -= 4


def draw_lead(draw: ImageDraw.ImageDraw, y: int, lead: str) -> None:
    """ピンクの小さな一言と、両脇の金のきらめき。"""
    font = load_font(38)
    draw.text((540, y), lead, font=font, fill=COLOR_ACCENT, anchor="ms")
    half = draw.textlength(lead, font=font) / 2
    for cx in (540 - half - 40, 540 + half + 40):
        draw_sparkle(draw, cx, y - 14, 14, COLOR_GOLD)


def draw_divider(draw: ImageDraw.ImageDraw, y: int) -> None:
    draw.line((340, y, 520, y), fill=COLOR_GOLD, width=2)
    draw.line((560, y, 740, y), fill=COLOR_GOLD, width=2)
    draw_heart(draw, 540, y, 12, COLOR_GOLD)


def draw_signature(draw: ImageDraw.ImageDraw, y: int, size: int = 46) -> None:
    draw.text((540, y), SIGNATURE, font=load_font(size), fill=COLOR_TEXT, anchor="ms")
    draw.text((540, y + int(size * 0.74)), SIGNATURE_SUB, font=load_font(size * 22 // 46), fill=COLOR_TEXT, anchor="ms")


def with_panel(image: Image.Image, text_layer: Image.Image) -> Image.Image:
    """文字のまとまりを画像の上下中央へ移し、まわりに余白をとって半透明のパネルを敷く。"""
    _, top, _, bottom = text_layer.getbbox()
    shift = (1350 - (bottom - top)) // 2 - top
    text_layer = text_layer.transform(image.size, Image.AFFINE, (1, 0, 0, 0, 1, -shift))
    panel_top, panel_bottom = top + shift - 80, bottom + shift + 80

    panel = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle((80, panel_top, 1000, panel_bottom), radius=40, fill=COLOR_PANEL)
    draw.rounded_rectangle((100, panel_top + 20, 980, panel_bottom - 20), radius=30,
                           outline=COLOR_GOLD + (170,), width=2)

    canvas = Image.alpha_composite(image.convert("RGBA"), panel)
    return Image.alpha_composite(canvas, text_layer).convert("RGB")


def render_cover(image: Image.Image, lead: str, headline: str, note: str = "") -> Image.Image:
    """表紙・締めの画像。小さな一言、大きな見出し、（あれば）補足、サロン名。"""
    lines = split_lines(headline)
    size, fonts = fit_heading(lines, 100, 52)
    line_height = int(size * 1.5)

    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    y = 200
    draw_lead(draw, y, lead)

    y += 40 + int(size * 1.15)
    for segments in lines:
        draw_line(draw, y, segments, fonts)
        y += line_height
    y -= line_height

    if note:
        y += 90
        draw.text((540, y), note, font=load_font(36), fill=COLOR_TEXT, anchor="ms")

    y += 70
    draw_divider(draw, y)
    draw_signature(draw, y + 76)
    return with_panel(image, layer)


NO_LINE_START = "、。，．・：；？！ー」』）】ぁぃぅぇぉっゃゅょァィゥェォッャュョ"


def wrap_text(text: str, font: ImageFont.FreeTypeFont, width: float) -> list[str]:
    """日本語を折り返す。行の長さをそろえて、最後の行に1〜2文字だけ残らないようにする。"""
    lines = wrap_greedy(text, font, width)
    if len(lines) > 1:
        balanced = wrap_greedy(text, font, font.getlength(text) / len(lines) * 1.08)
        if len(balanced) == len(lines):
            return balanced
    return lines


def wrap_greedy(text: str, font: ImageFont.FreeTypeFont, width: float) -> list[str]:
    """1文字ずつ詰めて折り返す。句読点などが行頭に来ないようにする。"""
    lines, current = [], ""
    for char in text:
        if current and font.getlength(current + char) > width and char not in NO_LINE_START:
            lines.append(current)
            current = ""
        current += char
    if current:
        lines.append(current)
    return lines


def render_list(image: Image.Image, title: str, items: list[str], numbered: bool) -> Image.Image:
    """中身の画像。見出しの下に、番号付き（またはハート付き）の箇条書きを並べる。"""
    title_lines = split_lines(title)
    title_size, title_fonts = fit_heading(title_lines, 68, 44)
    items = [re.sub(r"[【】]", "", item).strip() for item in items[:6]]

    # 本文が画像に収まるまで文字を小さくする
    text_x, text_width = 230, 720
    for size in range(42, 27, -2):
        font = load_font(size)
        wrapped = [wrap_text(item, font, text_width) for item in items]
        height = sum(len(lines) * size * 1.55 + size * 0.7 for lines in wrapped)
        if height <= 760:
            break

    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    y = 200 + int(title_size * 1.15)
    for segments in title_lines:
        draw_line(draw, y, segments, title_fonts)
        y += int(title_size * 1.45)
    y += 10 - int(title_size * 0.45)
    draw_divider(draw, y)

    y += 50
    number_font = load_font(int(size * 0.75))
    for index, lines in enumerate(wrapped, start=1):
        center_y = y + size - size * 0.36
        if numbered:
            r = size * 0.62
            draw.ellipse((170 - r, center_y - r, 170 + r, center_y + r), fill=COLOR_ACCENT)
            draw.text((170, center_y), str(index), font=number_font, fill=(255, 255, 255), anchor="mm")
        else:
            draw_heart(draw, 170, center_y, size * 0.36, COLOR_ACCENT)
        for line in lines:
            draw.text((text_x, y + size), line, font=font, fill=COLOR_TEXT, anchor="ls")
            y += int(size * 1.55)
        y += int(size * 0.7)

    y += 20
    draw_divider(draw, y)
    draw_signature(draw, y + 56, size=32)
    return with_panel(image, layer)


def cmd_generate(slot: str | None) -> None:
    now = datetime.now(JST)
    slot = slot or detect_slot(now)
    history = load_history()

    post = write_post(slot, now, history)
    print(f"テーマ: {post['theme']}\n\n{post['caption']}\n")

    POSTS_DIR.mkdir(exist_ok=True)
    background = make_background(post["image_prompt"])
    pages = [render_cover(background, post["cover"]["lead"], post["cover"]["headline"])]
    pages += [render_list(background, slide["title"], slide["items"], slide["numbered"])
              for slide in post["slides"][:3]]
    pages.append(render_cover(background, post["closing"]["lead"], post["closing"]["headline"],
                              note="ご予約・ご相談は DM から"))

    images = []
    for number, page in enumerate(pages, start=1):
        name = f"posts/{now:%Y%m%d}-{slot}-{number}.jpg"
        page.save(ROOT / name, "JPEG", quality=92)
        images.append(name)

    latest = {
        "date": now.strftime("%Y-%m-%d"),
        "slot": slot,
        "theme": post["theme"],
        "caption": post["caption"],
        "cover": post["cover"],
        "slides": post["slides"],
        "closing": post["closing"],
        "image_prompt": post["image_prompt"],
        "images": images,
        "published": False,
    }
    LATEST_FILE.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("保存しました:", ", ".join(images))


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
    urls = [f"https://raw.githubusercontent.com/{repo}/{sha}/{path}" for path in latest["images"]]

    user_id = env("IG_USER_ID")
    if len(urls) == 1:
        container = ig_request("POST", f"{user_id}/media", image_url=urls[0], caption=latest["caption"])
    else:
        # カルーセル: 1枚ずつ登録してから、まとめて1つの投稿にする
        children = []
        for url in urls:
            child = ig_request("POST", f"{user_id}/media", image_url=url, is_carousel_item="true")
            wait_until_ready(child["id"])
            children.append(child["id"])
        container = ig_request("POST", f"{user_id}/media", media_type="CAROUSEL",
                               children=",".join(children), caption=latest["caption"])
    wait_until_ready(container["id"])
    media = ig_request("POST", f"{user_id}/media_publish", creation_id=container["id"])
    print(f"投稿しました: media_id={media['id']}")

    latest["published"] = True
    latest["media_id"] = media["id"]
    LATEST_FILE.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    history = load_history()
    history.append({k: latest[k] for k in ("date", "slot", "theme", "images", "media_id")})
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
