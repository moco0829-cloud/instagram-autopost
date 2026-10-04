# Instagram 自動投稿

毎日 **夜19時** に、AIが作った画像と文章を Instagram に自動投稿する仕組みです。
投稿は、横にスワイプして見るカルーセル形式（表紙・中身1〜3枚・締めの3〜5枚）です。
GitHub Actions（クラウド）で動くので、パソコンの電源が切れていても投稿されます。

```
GitHub Actions（毎日19時に起動）
  ├─ Claude が「テーマ・キャプション・各画像の文字・画像の指示文」を作る（brand.md と過去の投稿履歴を参照）
  ├─ OpenAI の画像生成AIが背景画像を作る（1080×1350 の縦長に整える）
  ├─ 同じ背景の上に、表紙・中身（番号付きの手順やチェックリスト）・締めの文字を明朝体で重ねる
  ├─ 画像をリポジトリに保存（Instagram が読める公開URLにするため）
  ├─ Instagram API で投稿
  ├─ 同じ画像と文章を Facebook ページにも投稿（Secrets に FB_PAGE_ID と FB_PAGE_TOKEN があるときだけ）
  └─ 履歴を history.json に記録
```

## ファイル

| ファイル | 役割 |
| --- | --- |
| `brand.md` | **アカウントの発信方針。最初にここを書き換えてください。** |
| `autopost.py` | 文章・画像の生成と Instagram への投稿 |
| `history.json` | 投稿履歴（テーマの重複を避けるのに使う） |
| `posts/` | 生成した画像の保存先 |
| `.github/workflows/autopost.yml` | 毎日19時の自動実行 |
| `.github/workflows/refresh-token.yml` | Instagram トークンの自動延長（週1回） |

## 費用の目安（1日1投稿・月30投稿）

| 項目 | 目安 |
| --- | --- |
| Claude API（文章） | 月 数十円程度 |
| OpenAI 画像生成（medium 品質） | 月 200〜300円程度 |
| GitHub Actions | 公開リポジトリなら無料 |
| Instagram API | 無料 |

画像の品質は `IMAGE_QUALITY`（`low` / `medium` / `high`）で変えられ、料金も変わります。

---

## セットアップ手順

### 1. Instagram をプロアカウントに切り替える（無料）

個人アカウントは API から投稿できません。プロアカウントに切り替えます（フォロワーや投稿はそのまま残ります）。

1. Instagram アプリ →「プロフィール」→ 右上の「≡」→「アカウントの種類とツール」
2. 「プロアカウントに切り替える」→ カテゴリを選ぶ →「クリエイター」か「ビジネス」を選択

### 2. Meta のアプリを作り、アクセストークンを取得する

1. [Meta for Developers](https://developers.facebook.com/) に Facebook アカウントでログインし、開発者登録する
2. 「マイアプリ」→「アプリを作成」→ ユースケースで **「Instagram でメッセージやコンテンツを管理」** を選ぶ
3. アプリの「Instagram」→「Instagramログインによる API 設定」を開く
4. 「アクセストークンを生成」で自分の Instagram アカウントを追加し、ログインして許可する
   - 権限に `instagram_business_basic` と `instagram_business_content_publish` が含まれていることを確認
5. 表示された **アクセストークン**（長期トークン・60日有効）を控える
6. ブラウザで次のURLを開き、表示される `user_id` を控える（`トークン` の部分を置き換え）
   ```
   https://graph.instagram.com/v23.0/me?fields=user_id,username&access_token=トークン
   ```

### 3. API キーを用意する

- **Claude**: [Claude Console](https://console.anthropic.com/) → API Keys でキーを作成
- **OpenAI**: [OpenAI Platform](https://platform.openai.com/api-keys) でキーを作成し、支払い方法を登録
  - 画像生成モデルの利用に組織の本人確認（Verify Organization）を求められる場合があります

### 4. GitHub に公開リポジトリを作って、このフォルダを置く

Instagram API はインターネット上で公開されている画像URLしか読めないため、**公開（Public）リポジトリ** にします。
このフォルダの中身だけを入れた専用リポジトリにしてください（他の資料を一緒に公開しないため）。

```bash
cd instagram-autopost
git init
git add .
git commit -m "Instagram自動投稿の初期設定"
git branch -M main
git remote add origin https://github.com/あなたのユーザー名/instagram-autopost.git
git push -u origin main
```

### 5. GitHub の Secrets を登録する

リポジトリの「Settings」→「Secrets and variables」→「Actions」→「New repository secret」で次を登録します。

| 名前 | 中身 |
| --- | --- |
| `ANTHROPIC_API_KEY` | Claude の API キー |
| `OPENAI_API_KEY` | OpenAI の API キー |
| `IG_ACCESS_TOKEN` | 手順2-5 のアクセストークン |
| `IG_USER_ID` | 手順2-6 の `user_id` |
| `GH_PAT` | トークン自動延長用。下記参照 |
| `FB_PAGE_ID` | （任意）Facebook ページのID。登録すると Facebook にも同時投稿 |
| `FB_PAGE_TOKEN` | （任意）Facebook ページのアクセストークン（期限なしのもの） |

**`GH_PAT` の作り方**: [新しいトークンの作成画面（classic）](https://github.com/settings/tokens/new) を開き、
Note に `instagram-autopost-secrets`、Expiration に「No expiration」、Select scopes で「**repo**」にチェックを入れて作成します。
これがないと、Instagram のトークンが60日で切れて投稿が止まります。
（Fine-grained token では Secrets の許可を付けても書き込めないことがあったため、classic を使います。）

### 6. 試しに動かす

1. リポジトリの「Actions」タブ →「Instagram 自動投稿」→「Run workflow」
2. まずは **「dry_run」にチェック** して実行 → `posts/` に画像、`posts/latest.json` に文章ができるので確認
3. 内容に問題なければ、チェックを外して実行 → Instagram に実際に投稿されます

### 7. 自動投稿を始める

試し投稿の内容に問題がなければ、定期実行をオンにします。

1. リポジトリの「Settings」→「Secrets and variables」→「Actions」→「**Variables**」タブ
2. 「New repository variable」で、名前 `AUTOPOST_ENABLED`、値 `true` を登録

以降は毎日 19:00 に自動で投稿されます。止めたいときは、この値を `false` にします。

---

## よくある調整

- **投稿内容の方向性を変えたい** → `brand.md` を書き換えて push
- **投稿時刻を変えたい** → `.github/workflows/autopost.yml` の `cron`（UTC表記。日本時間から9時間引く）
- **画像の見出しの色を変えたい** → `autopost.py` の `COLOR_TEXT`（文字）・`COLOR_ACCENT`（強調のピンク）・`COLOR_GOLD`（金の飾り）
- **画像下のサロン名を変えたい** → workflow の生成ステップの `env` に `IMAGE_SIGNATURE` / `IMAGE_SIGNATURE_SUB` を追加
- **画像の品質・料金を変えたい** → workflow の生成ステップの `env` に `IMAGE_QUALITY: low` などを追加
- **一時停止したい** → Variables の `AUTOPOST_ENABLED` を `false` にする

## 注意点

- GitHub Actions の定期実行は混雑時に **数分〜30分ほど遅れる** ことがあります。
- 60日以上リポジトリに変更がないと定期実行が自動停止されますが、この仕組みは毎回画像をコミットするので通常は止まりません。
- 投稿は API 経由で毎回自動公開されます。AIが作った内容がそのまま出るため、最初の数日は投稿をチェックし、
  気になる点があれば `brand.md` の「投稿しないこと」に追記してください。
- Instagram API の投稿上限は 24時間で100件なので、1日1件は問題ありません。
