#!/usr/bin/env python3
"""Lit le catalogue PDF Hmarket, extrait les offres valables en Belgique
et écrit docs/promos.json + docs/img/*.jpg.

Usage : python scripts/build_promos.py [fichier.pdf]
Sans argument : télécharge le catalogue lié sur https://hmarket.fr/promotions/
"""
import io, json, os, re, sys, unicodedata, urllib.request
from collections import Counter
from datetime import date, datetime, timezone
import pdfplumber

SITE = "https://hmarket.fr/promotions/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_JSON = os.path.join(ROOT, "docs", "promos.json")
OUT_IMG = os.path.join(ROOT, "docs", "img")
UA = {"User-Agent": "Mozilla/5.0 (promos-perso; usage personnel)"}
DATE_RE = re.compile(r"(\d\d)/(\d\d)/(\d\d)")


def http(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()


def find_pdf_url():
    html = http(SITE).decode("utf-8", "replace")
    urls = re.findall(r'https?://[^"\'\s>]+\.pdf', html)
    urls = [u for u in urls if "catalogue" in u.lower() and "afrique" not in u.lower()]
    if not urls:
        raise SystemExit("Aucun PDF de catalogue trouvé sur la page promotions")
    urls.sort(key=lambda u: ("web" not in u.lower(), u))
    return urls[0]


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^a-z0-9 ]", "", "".join(c for c in s if not unicodedata.combining(c))).strip()


def to_date(d, m, y):
    return date(2000 + int(y), int(m), int(d))


def center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def box_gap(a, b):
    dx = max(a[0] - b[2], b[0] - a[2], 0)
    dy = max(a[1] - b[3], b[1] - a[3], 0)
    return dx, dy


def union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def wbox(w):
    return (w["x0"], w["top"], w["x1"], w["bottom"])


def clean_suffix(sfx):
    sfx = re.sub(r"\s+", " ", sfx or "").strip()
    sfx = re.sub(r"/\s*k\s*g", "/kg", sfx)
    sfx = re.sub(r"/\s*l\b", "/L", sfx)
    return sfx or None


def parse_page(page, pno):
    words = page.extract_words(extra_attrs=["size"], x_tolerance_ratio=0.25, y_tolerance=2)
    H = page.height
    used = set()

    # --- dates "Du dd/mm/yy au dd/mm/yy"
    dates = []
    for i, w in enumerate(words):
        if w["text"] == "Du" and i + 3 < len(words):
            a, b, c = words[i + 1], words[i + 2], words[i + 3]
            if DATE_RE.fullmatch(a["text"]) and b["text"] == "au" and DATE_RE.fullmatch(c["text"]):
                d1 = to_date(*DATE_RE.fullmatch(a["text"]).groups())
                d2 = to_date(*DATE_RE.fullmatch(c["text"]).groups())
                dates.append({"from": d1, "to": d2, "box": union([wbox(w), wbox(c)])})
                used.update(id(x) for x in (w, a, b, c))

    # --- pastilles "Le Lot de N"
    lots = []
    for i, w in enumerate(words):
        if w["text"] == "Lot" and w["size"] < 20:
            near = [x for x in words if abs(x["top"] - w["top"]) < 16 and abs(x["x0"] - w["x0"]) < 30 and x["size"] < 20]
            n = None
            for x in near:
                if x["text"].isdigit() and len(x["text"]) == 1 and x["top"] > w["top"] - 2 and x["x0"] > w["x0"] - 5:
                    n = int(x["text"])
                    used.add(id(x))
            for x in near:
                if x["text"] in ("Le", "Lot", "de"):
                    used.add(id(x))
            lots.append({"n": n or 2, "c": center(wbox(w))})

    # --- prix : gros chiffre + centimes (tailles variables selon les pages)
    prices = []
    bigs = [w for w in words if w["size"] >= 20 and re.fullmatch(r"\d{1,3}", w["text"]) and id(w) not in used]
    for bw in bigs:
        S = bw["size"]
        first = []
        for w in words:
            if w is bw or id(w) in used or not (0.3 * S <= w["size"] <= 0.8 * S):
                continue
            dx = w["x0"] - bw["x1"]
            dy = w["top"] - bw["top"]
            if -14 <= dx <= 80 and -0.1 * S <= dy <= 0.95 * S and re.match(r"^[.,€\d]", w["text"]):
                first.append((abs(dx) + abs(dy - 0.4 * S), w))
        if not first:
            continue
        start = min(first, key=lambda t: t[0])[1]
        chain = [start]
        changed = True
        while changed:
            changed = False
            for w in words:
                if w in chain or id(w) in used or w is bw or not (0.3 * S <= w["size"] <= 0.8 * S):
                    continue
                last = chain[-1]
                if abs(w["top"] - last["top"]) < 0.25 * S and -3 <= w["x0"] - last["x1"] < 30 and abs(w["size"] - last["size"]) < 5 and re.match(r"^[.,€\d/]|^(le|les|la|pièce|kg|l|la)\b", w["text"], re.I):
                    chain.append(w); changed = True; break
        tail = "".join(w["text"] for w in chain)
        m = re.match(r"^[.,]\s*(\d{1,2})\s*€?(.*)$", tail)
        if m:
            cents, rest = int(m.group(1).ljust(2, "0")), m.group(2)
        elif tail.startswith("€"):
            cents, rest = 0, tail[1:]
        else:
            continue
        parts = [bw] + chain
        used.add(id(bw)); used.update(id(w) for w in chain)
        extra = []
        ref = chain[0]
        for w in words:  # « le filet » / « de 1kg » : mots de même taille, à droite ou sous le prix
            if id(w) in used or abs(w["size"] - ref["size"]) > 3:
                continue
            same_line = abs(w["top"] - ref["top"]) < 0.25 * S and 0 <= w["x0"] - chain[-1]["x1"] < 90
            next_line = 0.3 * S <= w["top"] - ref["top"] < 1.6 * S and abs(w["x0"] - bw["x0"]) < 40
            wordlike = re.match(r"^(?:[A-Za-zÀ-ÿ/]|\d+\s?(?:kg|g|l|cl)\b)", w["text"]) and not re.fullmatch(r"[.,€\d]+", w["text"])
            if (same_line or next_line) and wordlike:
                extra.append(w); used.add(id(w)); parts.append(w)
        sfx = (rest + " " + " ".join(w["text"] for w in extra)).strip()
        prices.append({"value": round(int(bw["text"]) + cents / 100, 2), "suffix": sfx, "box": union([wbox(x) for x in parts]), "big": bw})

    # --- bruit de page : pied, en-tête
    body = [w for w in words if id(w) not in used and w["top"] < H - 70 and w["size"] < 30 or (id(w) not in used and w["top"] < H - 70 and w["size"] >= 30)]
    body = [w for w in body if id(w) not in used]

    # --- blocs de texte (union-find)
    body.sort(key=lambda w: (w["top"], w["x0"]))
    parent = list(range(len(body)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i

    for i, a in enumerate(body):
        for j in range(i + 1, len(body)):
            b = body[j]
            if b["top"] - a["top"] > 26:
                break
            same_line = abs(a["top"] - b["top"]) < 4 and -2 <= b["x0"] - a["x1"] < 12
            same_line = same_line or (abs(a["top"] - b["top"]) < 4 and -2 <= a["x0"] - b["x1"] < 12)
            below = 4 <= b["top"] - a["top"] <= 22 and min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]) > 0
            if same_line or below:
                parent[find(i)] = find(j)
    groups = {}
    for i, w in enumerate(body):
        groups.setdefault(find(i), []).append(w)
    blocks = []
    for g in groups.values():
        text = " ".join(w["text"] for w in sorted(g, key=lambda w: (round(w["top"] / 4), w["x0"])))
        if len(text) < 3 or "MANGERBOUGER" in text or "SANTÉ" in text:
            continue
        blocks.append({"words": g, "box": union([wbox(w) for w in g])})

    # --- association prix <-> blocs
    def cost(p, b):
        dx, dy = box_gap(p["box"], b["box"])
        lx = abs(p["box"][0] - b["box"][0])
        return dy * 1.5 + dx + 0.3 * lx

    pairs = sorted(((cost(p, b), pi, bi) for pi, p in enumerate(prices) for bi, b in enumerate(blocks)))
    taken_p, taken_b, assign = set(), set(), {}
    for c, pi, bi in pairs:
        if pi in taken_p or bi in taken_b or c > 260:
            continue
        taken_p.add(pi); taken_b.add(bi); assign.setdefault(pi, []).append(bi)
    for bi, b in enumerate(blocks):  # variantes d'un même prix
        if bi in taken_b:
            continue
        best = min(((cost(p, b), pi) for pi, p in enumerate(prices) if pi in assign), default=None)
        if best and best[0] < 110:
            assign[best[1]].append(bi); taken_b.add(bi)

    # --- images assignées à l'offre la plus proche
    imgs = [(im["x0"], im["top"], im["x1"], im["bottom"]) for im in page.images
            if (im["x1"] - im["x0"]) > 25 and (im["bottom"] - im["top"]) > 25
            and (im["x1"] - im["x0"]) * (im["bottom"] - im["top"]) < 0.22 * page.width * page.height]
    img_owner = {}
    for k, im in enumerate(imgs):
        best = None
        for pi, bis in assign.items():
            ob = union([prices[pi]["box"]] + [blocks[b]["box"] for b in bis])
            dx, dy = box_gap(im, ob)
            d = dx * 1.2 + dy
            if best is None or d < best[0]:
                best = (d, pi)
        if best and best[0] < 140:
            img_owner[k] = best[1]

    tiles = [(r["x0"], r["top"], r["x1"], r["bottom"]) for r in page.rects
             if r["width"] > 60 and r["height"] > 60 and r["width"] < page.width - 20 and r["height"] < page.height * 0.6]
    offers = []
    for pi, bis in assign.items():
        p = prices[pi]
        bl = [blocks[b] for b in bis]
        lines = []
        for b in bl:
            rows = {}
            for w in b["words"]:
                rows.setdefault(round(w["top"] / 3), []).append(w)
            for r in sorted(rows):
                ws = sorted(rows[r], key=lambda w: w["x0"])
                lines.append((" ".join(w["text"] for w in ws), max(w["size"] for w in ws), ws[0]["x0"]))
        text_all = " ".join(t for t, _, _ in lines)
        refs = re.findall(r"n°\s?(\d+)", text_all)
        origin = None
        mo = re.search(r"Origine\s+([A-ZÉÈÀÂÎÔÛÇ' -]+?)(?=\s+(?:n°|\d)|$)", text_all)
        if mo:
            origin = mo.group(1).strip().title()
        qty = unit_price = None
        mq = re.search(r"((?:\d+\s?x\s?)?\d[\d,.]*\s?(?:kg|g|cl|ml|L|l)\b)\s*-\s*([\d,]+\s?€\s*/\s*(?:kg|L|pièce))", text_all)
        if mq:
            qty, unit_price = mq.group(1), mq.group(2).replace(" ", "")
        name_lines = [t for t, s, _ in lines if s >= 11.5 and not re.match(r"^(Origine|n°|Suggestion|Disponible)", t) and not re.fullmatch(r"[A-ZÉÈ' -]{4,}", t)]
        name = " ".join(name_lines).strip() or text_all[:60]
        name = re.sub(r"\s+", " ", name)
        name = re.sub(r"(?:^|\s)[.,]\d{1,2}€?(?=\s|$)", " ", name).strip()
        toks = name.split()
        if toks and sum(len(t) <= 2 for t in toks) / len(toks) > 0.5 and len(toks) > 3:
            name = ""  # texte espacé illisible : on affichera l'image seule
        name = re.sub(r"^(?:à partir de\s+\d+\s?kg\s+)", "", name, flags=re.I)
        name = re.sub(r"\s+(?:à partir de\s+\d+\s?kg)$", "", name, flags=re.I)
        name = re.sub(r"^(?:e|é)\s+(?:[A-ZÉ-]{4,}\s+)+", "", name)
        name = re.sub(r"^[A-ZÉ-]{4,}( [A-ZÉ-]{3,})*\s+(?=[A-Z][a-zé])", "", name)
        # date : la plus proche au-dessus, sinon défaut de page
        pb = p["box"]
        dcands = []
        for d in dates:
            db = d["box"]
            dy = pb[1] - db[3]
            if dy >= -20:
                dcands.append((dy + 0.4 * abs(pb[0] - db[0]), d))
        d = min(dcands, key=lambda t: t[0])[1] if dcands else None
        crop_boxes = [p["box"]] + [b["box"] for b in bl] + [imgs[k] for k, o in img_owner.items() if o == pi]
        crop = union(crop_boxes)
        pc, bc = center(p["box"]), center(union([b["box"] for b in bl]))
        holders = [r for r in tiles if r[0] <= pc[0] <= r[2] and r[1] <= pc[1] <= r[3] and r[0] <= bc[0] <= r[2] and r[1] <= bc[1] <= r[3]]
        tile = min(holders, key=lambda r: (r[2] - r[0]) * (r[3] - r[1])) if holders else None
        if tile:
            crop = tile
        lot = None
        if lots:
            ob = union(crop_boxes)
            near = [l for l in lots if ob[0] - 25 <= l["c"][0] <= ob[2] + 25 and ob[1] - 25 <= l["c"][1] <= ob[3] + 25]
            if near:
                lot = near[0]["n"]
        offers.append({
            "page": pno, "name": name, "price": p["value"], "price_suffix": clean_suffix(p["suffix"]),
            "lot": lot, "qty": qty, "unit_price": unit_price, "origin": origin, "refs": refs,
            "date_from": d["from"].isoformat() if d else None,
            "date_to": d["to"].isoformat() if d else None,
            "_crop": crop, "_y": pb[1], "_x": pb[0], "_tile": bool(tile),
        })
    note = " ".join(w["text"] for w in words)
    return offers, note, dates


def ensure_icons():
    from PIL import Image, ImageDraw
    for n in (192, 512):
        fp = os.path.join(ROOT, "docs", f"icon-{n}.png")
        if os.path.exists(fp):
            continue
        im = Image.new("RGB", (n, n), "#d62828")
        d = ImageDraw.Draw(im)
        r = n * 0.11
        d.ellipse([n * .22, n * .22, n * .22 + 2 * r, n * .22 + 2 * r], outline="white", width=int(n * .05))
        d.ellipse([n * .78 - 2 * r, n * .78 - 2 * r, n * .78, n * .78], outline="white", width=int(n * .05))
        d.line([n * .72, n * .26, n * .28, n * .74], fill="white", width=int(n * .06))
        im.save(fp)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path:
        data, src = open(path, "rb").read(), path
    else:
        src = find_pdf_url(); data = http(src)
    os.makedirs(OUT_IMG, exist_ok=True)
    ensure_icons()
    all_offers, excl_rules, all_dates = [], [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            offers, note, dates = parse_page(page, pno)
            all_dates += [d for d in dates]
            for m in re.finditer(r"[Oo]ffres\s+(\w+)\s+non\s+valables?\s+dans\s+les\s+magasins\s+de\s+(.+?)\.(?:\s|$)", note):
                excl_rules.append({"page": pno, "category": m.group(1), "stores": m.group(2)})
            for o in offers:
                o["_page_obj"] = page
            all_offers += offers
        # pages d'exclusion Bruxelles
        brux_pages = {r["page"] for r in excl_rules if re.search(r"bruxelles", r["stores"], re.I)}
        excluded_names = {norm(o["name"]) for o in all_offers if o["page"] in brux_pages}
        scale = 2.0
        images = {}
        for o in all_offers:
            if o["page"] not in images:
                images[o["page"]] = o["_page_obj"].to_image(resolution=72 * scale).original.convert("RGB")
        best = {}
        for o in all_offers:
            k = ("ref" + o["refs"][0]) if o["refs"] else (norm(o["name"])[:40], o["price"])
            if k not in best or (best[k]["page"] == 1 and o["page"] > 1):
                best[k] = o
        all_offers = list(best.values())
        old_imgs = [os.path.join(OUT_IMG, f) for f in os.listdir(OUT_IMG)]
        result = []
        for i, o in enumerate(sorted(all_offers, key=lambda o: (o["page"], round(o["_y"] / 40), o["_x"])), 1):
            n = norm(o["name"])
            why = None
            if o["page"] in brux_pages:
                why = "Offre boucherie non valable dans les magasins de Bruxelles (mention du catalogue)"
            elif any(n.startswith(e[:14]) or e.startswith(n[:14]) for e in excluded_names if len(e) > 8 and len(n) > 8):
                why = "Même produit que l'offre boucherie non valable à Bruxelles"
            x0, y0, x1, y1 = o["_crop"]
            im = images[o["page"]]
            pad = 0 if o["_tile"] else 6
            box = [max(0, int((x0 - pad) * scale)), max(0, int((y0 - pad) * scale)),
                   min(im.width, int((x1 + pad) * scale)), min(im.height, int((y1 + pad) * scale))]
            fn = f"p{o['page']}-{i:02d}.jpg"
            im.crop(box).save(os.path.join(OUT_IMG, fn), quality=82)
            result.append({
                "id": f"s{o['page']}-{i:02d}", "page": o["page"], "name": o["name"], "price": o["price"],
                "price_suffix": o["price_suffix"], "lot": o["lot"], "qty": o["qty"], "unit_price": o["unit_price"],
                "origin": o["origin"], "refs": o["refs"], "date_from": o["date_from"], "date_to": o["date_to"],
                "image": "img/" + fn, "belgium": why is None, "excluded_reason": why,
                "tile": o["_tile"], "needs_check": bool(not o["date_from"] or not o["_tile"] or not o["name"]),
            })
    if len(result) < 5:
        raise SystemExit("Moins de 5 offres lues : catalogue illisible, données précédentes conservées")
    keep = {os.path.join(OUT_IMG, os.path.basename(o["image"])) for o in result}
    for f in old_imgs:
        if f not in keep and os.path.exists(f):
            os.remove(f)
    froms = [d["from"] for d in all_dates]; tos = [d["to"] for d in all_dates]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": src,
        "catalogue_from": min(froms).isoformat() if froms else None,
        "catalogue_to": max(tos).isoformat() if tos else None,
        "rules": excl_rules,
        "disclaimer": "Offres du catalogue Hmarket France/Belgique, hors offres exclues pour Bruxelles. Dans la limite des stocks, magasins participants.",
        "offers": result,
    }
    try:
        old = json.load(open(OUT_JSON, encoding="utf-8"))
        if old.get("offers") == out["offers"] and old.get("catalogue_to") == out["catalogue_to"]:
            out["generated_at"] = old.get("generated_at", out["generated_at"])
    except Exception:
        pass
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    be = [o for o in result if o["belgium"]]
    print(f"{len(result)} offres lues, {len(be)} valables en Belgique, {len(result)-len(be)} exclues")


if __name__ == "__main__":
    main()
