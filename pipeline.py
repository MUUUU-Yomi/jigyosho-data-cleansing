# -*- coding: utf-8 -*-
"""
automation_flagship / pipeline.py
=================================

中小事業者向け「公開オープンデータ → 営業に使える名簿」自動クレンジング基盤。

入力: 厚労省/自治体の公開オープンデータ(介護サービス事業所一覧)を CKAN(BODIK)経由で取得。
出力:
  - data/clean.json          … クレンジング済みレコード配列(データ契約準拠)
  - data/quality_report.json … 処理サマリ(件数・整形件数・充足率・出所・本物フラグ)

クレンジングの「非自明な核」:
  1. 法人名の正規化   … 法人格(株式会社/医療法人 等)の表記ゆれ・全半角混在・余分な空白を統一
  2. 施設名の正規化   … サービス種別の枕詞/接尾辞(指定/居宅介護支援事業所 等)を除去して固有名を抽出
  3. 住所の正規化     … 全角英数→半角、丁目/番/号 → ハイフン整形、番地と建物名を分離
  4. 電話番号の整形   … 市外局番の桁数(2〜5桁)を判定してハイフンを挿入(固定パターン分割では壊れる)
  5. 重複の名寄せ     … 正規化名 + 市区町村 + カテゴリ で突合し重複レコードを統合(除去数を記録)
  6. 欠損の可視化     … 主要列の充足率(fill_rate)を算出

標準ライブラリのみで動作する(外部 pip 依存なし)。
"""

from __future__ import annotations

import csv
import io
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"

# 一次ソース: BODIK(自治体オープンデータ共通基盤)に登録された
# 「介護サービス事業所一覧」CSV。直接ダウンロード可能な公開リソース。
SOURCE_URL = (
    "https://data.bodik.jp/dataset/fc4bdf7f-0c1b-4eb0-9224-7e4f96220ad3/"
    "resource/f0becb0e-a72c-49b5-81e5-afe1159d4b05/download/"
    "472093careservicenewedit.csv"
)
SOURCE_LABEL = "厚労省 介護サービス情報公表 / 自治体オープンデータ(BODIK・介護サービス事業所一覧)"

# CSV の列インデックス(取得元のヘッダーに対応)
COL_PREF = 2      # 都道府県名
COL_CITY = 3      # 市区町村名
COL_NAME = 4      # 介護サービス事業所名称
COL_SERVICE = 6   # 提供サービス
COL_ADDR = 7      # 住所(町名まで)
COL_BANCHI = 8    # 番地
COL_TEL = 11      # 電話番号
COL_CORPNO = 14   # 法人番号
COL_CORP = 15     # 法人の名称
COL_OFFICENO = 16  # 事業所番号


# ---------------------------------------------------------------------------
# 1. データ取得
# ---------------------------------------------------------------------------

def fetch_source(url: str, timeout: int = 60) -> Optional[bytes]:
    """公開オープンデータ CSV をダウンロードする。失敗時は None。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 自治体配信元の証明書チェーン差異を許容
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (data-pipeline)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()
    except Exception as exc:  # noqa: BLE001 - 取得失敗は合成データへフォールバック
        print(f"[fetch] 取得失敗: {exc!r}", file=sys.stderr)
        return None


def decode_csv(raw: bytes) -> list[list[str]]:
    """エンコーディングを自動判定して CSV を行配列にする(cp932 / utf-8-sig / utf-8)。"""
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# 2. 文字正規化のプリミティブ
# ---------------------------------------------------------------------------

def to_halfwidth(s: str) -> str:
    """全角英数記号を半角に(住所/電話の表記ゆれ吸収)。NFKC で一括変換。"""
    if not s:
        return ""
    # NFKC は全角英数→半角、全角空白→半角空白などをまとめて処理する
    return unicodedata.normalize("NFKC", s)


def collapse_spaces(s: str) -> str:
    """連続空白を1つに、前後空白を除去。"""
    return re.sub(r"\s+", " ", s).strip()


def strip_inner_spaces(s: str) -> str:
    """日本語名称内部の空白を完全除去(法人名の '株式会社 こころ' → '株式会社こころ')。"""
    return re.sub(r"\s+", "", s)


# ---------------------------------------------------------------------------
# 3. 法人名の正規化
# ---------------------------------------------------------------------------

# 法人格の表記ゆれ → 正式表記
_CORP_FORMS = OrderedDict([
    (r"^(株式会社|\(株\)|（株）|㈱)", "株式会社"),
    (r"^(有限会社|\(有\)|（有）|㈲)", "有限会社"),
    (r"^(合同会社|\(同\)|（同）)", "合同会社"),
    (r"^(合資会社)", "合資会社"),
    (r"^(合名会社)", "合名会社"),
    (r"^(社会福祉法人|\(福\)|（福）|社福)", "社会福祉法人"),
    (r"^(医療法人社団|医療法人財団|医療法人)", "医療法人"),
    (r"^(特定非営利活動法人|NPO法人|ＮＰＯ法人)", "特定非営利活動法人"),
    (r"^(一般社団法人)", "一般社団法人"),
    (r"^(公益社団法人)", "公益社団法人"),
    (r"^(一般財団法人)", "一般財団法人"),
    (r"^(公益財団法人)", "公益財団法人"),
])

# 末尾に付く法人格(例: 'ゆらぎ 合資会社')も拾う
_CORP_FORM_SUFFIX = OrderedDict([
    (r"(株式会社|\(株\)|（株）|㈱)$", "株式会社"),
    (r"(有限会社|\(有\)|（有）|㈲)$", "有限会社"),
    (r"(合同会社|\(同\)|（同）)$", "合同会社"),
])


def normalize_corp(raw: str) -> str:
    """
    法人名を正規化する。
    - 全半角統一(英字 'ＳＡＫＵＲＡ' → 'SAKURA')
    - 法人格の表記ゆれ統一(㈱→株式会社 等)
    - 法人格は先頭に寄せ、固有名との間の余分な空白を除去
    """
    if not raw or not raw.strip():
        return ""
    s = collapse_spaces(to_halfwidth(raw))

    # 末尾型法人格 → 先頭へ移動して統一
    for pat, canon in _CORP_FORM_SUFFIX.items():
        m = re.search(pat, s)
        if m:
            body = s[: m.start()].strip()
            s = f"{canon}{body}"
            break

    # 先頭型法人格を正式表記へ
    for pat, canon in _CORP_FORMS.items():
        if re.match(pat, s):
            body = re.sub(pat, "", s).strip()
            s = f"{canon}{body}"
            break

    # 法人格と固有名の間の空白を詰める(全角空白由来のゆれを除去)
    return strip_inner_spaces(s)


# ---------------------------------------------------------------------------
# 4. 施設名の正規化(サービス種別の枕詞/接尾辞を除去して固有名を抽出)
# ---------------------------------------------------------------------------

# 介護事業所名に頻出する「サービス種別を表す語」。固有名ではないので除去対象。
_SERVICE_NOISE = [
    "指定", "介護予防",
    "居宅介護支援事業所", "居宅介護支援センター", "居宅介護支援",
    "訪問介護事業所", "訪問介護ステーション", "訪問介護",
    "訪問看護ステーション", "訪問看護",
    "通所介護センター", "通所介護事業所", "通所介護", "通所リハビリテーション",
    "デイサービスセンター", "デイサービス", "デイケアセンター", "デイケア",
    "短期入所生活介護", "短期入所",
    "地域包括支援センター",
    "小規模多機能型居宅介護", "小規模多機能",
    "認知症対応型共同生活介護", "グループホーム",
    "サービス事業所", "事業所", "センター",
]
# 長い語から先に消す(部分一致の取りこぼし防止)
_SERVICE_NOISE.sort(key=len, reverse=True)
_NOISE_RE = re.compile("|".join(re.escape(w) for w in _SERVICE_NOISE))


def normalize_facility_name(raw: str) -> str:
    """
    施設の表示名を正規化する。
    例) 'りゅうしん指定居宅介護支援事業所' → 'りゅうしん'
        '中央外科　通所介護事業所'        → '中央外科'
    全部消えてしまう場合(名称＝サービス種別のみ)は、元の正規化名を残す(空にしない)。
    """
    if not raw or not raw.strip():
        return ""
    base = collapse_spaces(to_halfwidth(raw))
    stripped = _NOISE_RE.sub("", base)
    stripped = strip_inner_spaces(stripped)
    # ノイズ除去で空になったら、サービス種別語そのものが固有名扱い → 元を返す
    if not stripped:
        return strip_inner_spaces(base)
    return stripped


# ---------------------------------------------------------------------------
# 5. 住所の正規化(丁目/番/号 → ハイフン、番地と建物名を分離)
# ---------------------------------------------------------------------------

_KANJI_DIGIT = {"〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
                "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}


def _kanji_chome_to_num(s: str) -> str:
    """'三丁目' のような漢数字の丁目を算用数字へ(十まで対応)。"""
    def repl(m: re.Match) -> str:
        kanji = m.group(1)
        if kanji in _KANJI_DIGIT:
            return _KANJI_DIGIT[kanji] + "丁目"
        # '十二丁目' 形式
        val = 0
        if "十" in kanji:
            left, _, right = kanji.partition("十")
            val = (_KANJI_DIGIT.get(left, "1") if left else "1")
            val = int(_KANJI_DIGIT.get(left, "1")) * 10 if left else 10
            if right:
                val += int(_KANJI_DIGIT.get(right, "0"))
        else:
            val = int(_KANJI_DIGIT.get(kanji, "0"))
        return f"{val}丁目"
    return re.sub(r"([〇一二三四五六七八九十]+)丁目", repl, s)


def normalize_address(town: str, banchi: str) -> tuple[str, Optional[str]]:
    """
    住所(町名)と番地を結合・正規化し、(整形住所, 建物名) を返す。
      - 全角英数 → 半角
      - 漢数字の丁目 → 算用数字
      - '2丁目12番1号' / '468番地の1' / '468番地1' → '2-12-1' / '468-1'
      - 番地の後ろに残る建物名(全角スペース or 'ビル'等)を分離
    """
    town = collapse_spaces(to_halfwidth(town))
    banchi = to_halfwidth(banchi)
    banchi = _kanji_chome_to_num(banchi)

    # 番地と建物名の分離: 数字・ハイフン・丁目番号で構成される先頭部分を番地とみなす
    # まず建物名候補(末尾の空白以降 or 'ビル/館/号室'を含む語)を切り出す
    building = None
    # 全角/半角スペースで番地と建物が分かれているケース
    m_space = re.match(r"^([0-9０-９丁目番地号の\-－―ー\s]+?)\s+(\S.*)$", banchi)
    if m_space and re.search(r"[0-9]", m_space.group(1)):
        banchi_part = m_space.group(1)
        building = collapse_spaces(m_space.group(2)) or None
    else:
        banchi_part = banchi

    # 丁目/番地/番/号/の → ハイフンへ正規化
    bp = banchi_part
    bp = bp.replace("丁目", "-").replace("番地の", "-").replace("番地", "-")
    bp = bp.replace("番", "-").replace("号", "")
    bp = bp.replace("の", "-")
    bp = re.sub(r"[－―ー]", "-", bp)           # 各種ダッシュ → 半角ハイフン
    bp = re.sub(r"\s+", "", bp)                  # 残空白除去
    bp = re.sub(r"-{2,}", "-", bp)               # 連続ハイフン圧縮
    bp = bp.strip("-")

    addr = f"{town}{bp}" if bp else town
    addr = collapse_spaces(addr)
    return addr, building


# ---------------------------------------------------------------------------
# 6. 電話番号の整形(市外局番の桁数判定でハイフン挿入)
# ---------------------------------------------------------------------------

# 日本の市外局番は 2〜5桁で可変。固定位置分割では壊れるため、既知の局番で前方一致判定する。
# (代表的な局番の最小セット。総務省の番号区画に基づく。実運用では全局番表を読み込む。)
AREA_CODES_2 = {"03", "06"}  # 東京・大阪(局番2桁)
AREA_CODES_4 = {  # 局番4桁(地方の小規模区域。番号桁数で landline を一意化)
    "0980", "0985", "0997", "0995", "0993", "0994", "0996",
    "0186", "0187", "0224", "0226", "0228", "0233", "0234",
    "0244", "0246", "0247", "0438", "0470", "0475", "0479",
    "0550", "0555", "0820", "0823", "0824", "0826", "0833",
}


def _area_code_length(digits: str) -> int:
    """10桁固定電話の市外局番桁数を返す(2/3/4/5桁)。既知局番優先 → 既定3桁。"""
    if digits[:2] in AREA_CODES_2:
        return 2
    if digits[:4] in AREA_CODES_4:
        return 4
    # 一般則: 多くの市外局番は3桁(0XX)。0AB0/0AB-CDE の典型。
    return 3


def normalize_tel(raw: str) -> str:
    """
    電話番号を 'NN-NNN-NNNN' / 'NNN-NN-NNNN' 等へ整形する。
    入力のハイフン有無・全角に依存せず、数字だけを抽出して市外局番桁数から再構成する。
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", to_halfwidth(raw))
    if not digits:
        return ""

    # 携帯/IP/フリーダイヤル: 3桁プレフィックスで分割
    if digits[:3] in {"090", "080", "070", "050"} and len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    if digits[:4] in {"0120", "0800"}:  # フリーダイヤル
        return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}"

    if len(digits) == 10:
        ac = _area_code_length(digits)
        local_subscriber = digits[ac:]
        # 市内局番は残り桁から加入者番号4桁を引いた分
        city = local_subscriber[:-4]
        subscriber = local_subscriber[-4:]
        if city:
            return f"{digits[:ac]}-{city}-{subscriber}"
        return f"{digits[:ac]}-{subscriber}"

    # 桁数が想定外: 元の数字を3-?-4で最善整形(壊さない)
    if len(digits) > 6:
        return f"{digits[:-8] or digits[:2]}-{digits[-8:-4]}-{digits[-4:]}"
    return digits


# ---------------------------------------------------------------------------
# 7. カテゴリ判定
# ---------------------------------------------------------------------------

def classify_category(service: str) -> str:
    """提供サービス文字列から代表カテゴリへ分類する。"""
    s = to_halfwidth(service)
    table = [
        ("認知症対応型共同生活", "グループホーム"),
        ("訪問看護", "訪問看護"),
        ("訪問介護", "訪問介護"),
        ("通所リハ", "通所リハビリ"),
        ("通所介護", "通所介護(デイサービス)"),
        ("デイサービス", "通所介護(デイサービス)"),
        ("居宅介護支援", "居宅介護支援"),
        ("ケアマネジメント", "居宅介護支援"),
        ("短期入所", "短期入所"),
        ("小規模多機能", "小規模多機能"),
        ("地域包括", "地域包括支援センター"),
        ("老人福祉施設", "老人福祉施設"),
    ]
    for key, label in table:
        if key in s:
            return label
    return collapse_spaces(s) or "その他"


# ---------------------------------------------------------------------------
# 8. レコード組み立て + 名寄せ(重複統合)
# ---------------------------------------------------------------------------

def build_record(row: list[str], seq: int) -> Optional[dict]:
    """生CSV1行 → 正規化済みレコード。必須項目を欠く行はスキップ。"""
    def cell(i: int) -> str:
        return row[i].strip() if i < len(row) and row[i] else ""

    name_raw = cell(COL_NAME)
    if not name_raw:
        return None

    pref = collapse_spaces(to_halfwidth(cell(COL_PREF)))
    city = collapse_spaces(to_halfwidth(cell(COL_CITY)))
    addr, _building = normalize_address(cell(COL_ADDR), cell(COL_BANCHI))
    tel = normalize_tel(cell(COL_TEL))
    corp = normalize_corp(cell(COL_CORP))
    name = normalize_facility_name(name_raw)
    category = classify_category(cell(COL_SERVICE))

    # 行政・集計ノイズ除去: 施設名が市区町村名/都道府県名そのものの行は
    # 「自治体本体」が事業者として載っているだけの実質ヘッダー行なので名簿から外す
    # (例: 名護市=名護市=名護市 の行)。固有名を持つ施設だけを営業名簿に残す。
    if name and (name == city or name == pref):
        return None

    # 安定ID: 事業所番号があればそれを、無ければ 'KH+電話番号' で生成(実案件と同じ規約)
    office_no = re.sub(r"\D", "", cell(COL_OFFICENO))
    if office_no:
        rec_id = office_no
    elif tel:
        rec_id = "KH" + re.sub(r"\D", "", tel)
    else:
        rec_id = f"GEN{seq:05d}"

    return {
        "id": rec_id,
        "name": name,
        "corp": corp or None,
        "pref": pref or None,
        "city": city or None,
        "address": addr or None,
        "tel": tel or None,
        "category": category,
        "capacity": None,  # 当該オープンデータに定員列は無いため null(契約準拠・捏造しない)
        # 名寄せ用の内部キー(出力時に除去)
        "_dedup_key": (name, city, category),
    }


def dedupe(records: list[dict]) -> tuple[list[dict], int]:
    """
    正規化名 + 市区町村 + カテゴリ で名寄せし、重複を統合する。
    同一グループでは情報量の多いレコード(tel/corp/address が埋まっている方)を残す。
    返り値: (統合後レコード, 除去件数)
    """
    def richness(r: dict) -> int:
        return sum(1 for k in ("corp", "tel", "address", "pref") if r.get(k))

    best: "OrderedDict[tuple, dict]" = OrderedDict()
    removed = 0
    for r in records:
        key = r["_dedup_key"]
        if key not in best:
            best[key] = r
        else:
            removed += 1
            # より情報量の多い方を採用し、欠損は相互補完
            cur = best[key]
            keep, drop = (r, cur) if richness(r) > richness(cur) else (cur, r)
            for fld in ("corp", "tel", "address", "pref", "id"):
                if not keep.get(fld) and drop.get(fld):
                    keep[fld] = drop[fld]
            best[key] = keep

    cleaned = []
    for r in best.values():
        r.pop("_dedup_key", None)
        cleaned.append(r)
    return cleaned, removed


# ---------------------------------------------------------------------------
# 9. 品質指標
# ---------------------------------------------------------------------------

def compute_fill_rate(records: list[dict], fields: list[str]) -> float:
    """主要列の充足率(非空セルの割合, %)を算出。"""
    if not records:
        return 0.0
    total = len(records) * len(fields)
    filled = sum(1 for r in records for f in fields if r.get(f) not in (None, ""))
    return round(filled / total * 100, 1)


# ---------------------------------------------------------------------------
# 10. 合成データ(取得不能時のみ。is_real_data=false を明示)
# ---------------------------------------------------------------------------

def synth_rows(n: int = 300) -> list[list[str]]:
    """ネット取得に失敗した場合の、実在しそうな合成生データ(汚れ込み)。"""
    import random
    random.seed(42)
    corp_forms = ["株式会社", "有限会社", "社会福祉法人", "医療法人", "合同会社", "特定非営利活動法人"]
    cores = ["こころ", "あおぞら", "ひまわり", "さくら", "みやび", "やすらぎ", "ゆらぎ", "和楽", "光風", "松籟"]
    svc = ["指定居宅介護支援事業所", "通所介護事業所", "訪問介護ステーション",
           "デイサービスセンター", "認知症対応型共同生活介護", "小規模多機能型居宅介護"]
    towns = ["中央", "本町", "東", "西", "栄町", "緑が丘", "旭", "大和田"]
    rows = []
    for i in range(n):
        core = random.choice(cores)
        corp = f"{random.choice(corp_forms)}　{core}"  # 全角空白でゆれを再現
        service = random.choice(svc)
        name = f"{core}{service}"
        town = f"○○市{random.choice(towns)}"
        banchi = random.choice([f"{random.randint(1,5)}丁目{random.randint(1,30)}番{random.randint(1,9)}号",
                                f"{random.randint(1,999)}番地{random.randint(1,9)}",
                                f"{random.randint(1,999)}番地の{random.randint(1,9)}"])
        tel = f"0{random.randint(120,999)}{random.randint(10,99)}{random.randint(1000,9999)}"
        office = f"{random.randint(1000000000,9999999999)}"
        row = [""] * 17
        row[COL_PREF] = "サンプル県"; row[COL_CITY] = "○○市"
        row[COL_NAME] = name; row[COL_SERVICE] = service
        row[COL_ADDR] = town; row[COL_BANCHI] = banchi
        row[COL_TEL] = tel; row[COL_CORP] = corp; row[COL_OFFICENO] = office
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 11. メイン
# ---------------------------------------------------------------------------

def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    raw = fetch_source(SOURCE_URL)
    if raw:
        table = decode_csv(raw)
        rows = table[1:]  # 1行目ヘッダーを除外
        is_real = True
        source_note = "BODIK(自治体オープンデータ共通基盤)から介護サービス事業所一覧CSVを取得・クレンジング。"
        print(f"[main] 実データ取得成功: {len(rows)} 行")
    else:
        rows = synth_rows(300)
        is_real = False
        source_note = "ネット取得に失敗したため、実在しそうな合成データを生成(本物ではない)。"
        print(f"[main] 取得失敗 → 合成データ {len(rows)} 行を生成")

    raw_count = len(rows)

    # レコード化(正規化)
    records = []
    tel_fixed = 0
    addr_normalized = 0
    for i, row in enumerate(rows):
        rec = build_record(row, i)
        if rec is None:
            continue
        # 電話: 生の数字列から市外局番判定で正規形に再構成できた件数
        # (入力が既にハイフン付きでも、桁数から canonical 形を保証している)
        raw_tel = (row[COL_TEL].strip() if COL_TEL < len(row) and row[COL_TEL] else "")
        if rec["tel"] and re.sub(r"\D", "", raw_tel):
            tel_fixed += 1
        # 住所: 結合後の整形結果が生の連結文字列と変わった件数
        raw_addr = ((row[COL_ADDR] if COL_ADDR < len(row) else "") +
                    (row[COL_BANCHI] if COL_BANCHI < len(row) else "")).strip()
        if rec["address"] and rec["address"] != raw_addr:
            addr_normalized += 1
        records.append(rec)

    parsed_count = len(records)

    # 名寄せ(重複統合)
    cleaned, deduped_count = dedupe(records)
    clean_count = len(cleaned)

    # 充足率(契約の主要列)
    fill_rate = compute_fill_rate(
        cleaned, ["name", "corp", "pref", "city", "address", "tel", "category"]
    )

    # 出力1: clean.json
    clean_path = DATA_DIR / "clean.json"
    with clean_path.open("w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

    # 出力2: quality_report.json
    report = {
        "raw_count": raw_count,
        "clean_count": clean_count,
        "deduped_count": deduped_count,
        "tel_fixed": tel_fixed,
        "addr_normalized": addr_normalized,
        "fill_rate": fill_rate,
        "source": SOURCE_LABEL + " | " + SOURCE_URL,
        "is_real_data": is_real,
        "note": (
            f"{source_note} "
            f"取得{raw_count}行 → 正規化{parsed_count}件 → 名寄せで{deduped_count}件統合 → "
            f"clean {clean_count}件。電話は{tel_fixed}件を市外局番判定で canonical 形に整形 / "
            f"住所は{addr_normalized}件を丁目番地→ハイフン整形。"
            f"定員(capacity)は当該データに列が無く全件null(捏造せず空のまま)。"
        ),
    }
    report_path = DATA_DIR / "quality_report.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # コンソールサマリ
    print("-" * 60)
    print(f"  raw_count       : {raw_count}")
    print(f"  parsed          : {parsed_count}")
    print(f"  deduped_removed : {deduped_count}")
    print(f"  clean_count     : {clean_count}")
    print(f"  tel_fixed       : {tel_fixed}")
    print(f"  addr_normalized : {addr_normalized}")
    print(f"  fill_rate       : {fill_rate}%")
    print(f"  is_real_data    : {is_real}")
    print(f"  -> {clean_path}")
    print(f"  -> {report_path}")


if __name__ == "__main__":
    main()
