from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename
import os
import uuid
import statistics

from vinted_client import search_items
from claude_client import analyze_clothing

# ── Sale score helpers ────────────────────────────────────

_SCORE_LABELS = [
    (8, "Forte demande, peu de concurrence — excellente semaine pour vendre"),
    (6, "Bon moment pour publier — marché actif avec quelques concurrents"),
    (4, "Marché modéré — soignez votre description et vos photos"),
    (0, "Forte concurrence — misez sur un prix attractif et de belles photos"),
]


def _compute_sale_score(price_stats: dict, total_entries: int) -> dict:
    if not price_stats or price_stats.get("count", 0) < 3:
        return {"score": 5, "label": "Données insuffisantes pour calculer un score", "color": "orange"}

    n     = price_stats["count"]
    mean  = price_stats["mean"]
    rec   = price_stats["recommended_price_7j"]
    min_p = price_stats["min"]

    # Competition (0–4 pts): fewer total listings on Vinted = less competition
    if total_entries < 30:      comp = 4
    elif total_entries < 100:   comp = 3
    elif total_entries < 300:   comp = 2
    elif total_entries < 800:   comp = 1
    else:                       comp = 0

    # Price attractiveness (0–3 pts): how far below market average the rec price sits
    ratio = rec / mean if mean > 0 else 1.0
    if ratio < 0.55:    price = 3
    elif ratio < 0.70:  price = 2
    elif ratio < 0.85:  price = 1
    else:               price = 0

    # Market vitality (0–3 pts): active market with healthy price spread = real demand
    spread = (mean - min_p) / min_p if min_p > 0 else 0
    if n >= 30 and spread > 0.5:    vitality = 3
    elif n >= 20 or spread > 0.3:   vitality = 2
    elif n >= 10:                    vitality = 1
    else:                            vitality = 0

    score = min(10, comp + price + vitality)
    label = next(lbl for threshold, lbl in _SCORE_LABELS if score >= threshold)
    color = "green" if score > 7 else ("orange" if score >= 5 else "red")
    return {"score": score, "label": label, "color": color}


def _best_publish_time(price_stats: dict) -> dict:
    rec = price_stats.get("recommended_price_7j", 0) if price_stats else 0
    # Heuristic based on Vinted FR buyer activity patterns by price tier
    if rec > 50:
        return {"jour": "Jeudi", "creneau": "19h – 21h"}
    elif rec > 20:
        return {"jour": "Dimanche", "creneau": "18h – 20h"}
    else:
        return {"jour": "Samedi", "creneau": "10h – 12h"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB (up to 5 photos)
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory("static", "sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/manifest.json")
def manifest():
    resp = send_from_directory("static", "manifest.json")
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


@app.route("/search", methods=["POST"])
def search():
    """
    Accepts multipart/form-data with:
      - image  : (optional) image file of the clothing item
      - query  : (required) text description / search query
      - page   : (optional, default 1)
      - per_page: (optional, default 20, max 100)

    Returns JSON with Vinted search results.
    """
    query = request.form.get("query", "").strip()
    if not query:
        return jsonify({"error": "Le champ 'query' est obligatoire."}), 400

    page = int(request.form.get("page", 1))
    per_page = min(int(request.form.get("per_page", 20)), 100)

    # Save the uploaded image if provided (future use: image search / preview)
    image_path = None
    if "image" in request.files:
        file = request.files["image"]
        if file and file.filename and _allowed_file(file.filename):
            ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
            filename = f"{uuid.uuid4().hex}.{ext}"
            image_path = os.path.join(UPLOAD_FOLDER, filename)
            file.save(image_path)

    try:
        results = search_items(query, per_page=per_page, page=page)
    except Exception as exc:
        return jsonify({"error": f"Erreur lors de la recherche Vinted : {exc}"}), 502

    results["query"] = query
    results["image_uploaded"] = image_path is not None
    return jsonify(results)


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    POST multipart/form-data:
      - image : (optional) photo of the clothing item
      - query : (optional) text description

    At least one of image or query is required.

    Returns JSON with:
      - analysis     : Prizzy IA vision analysis (brand, type, color, size, etc.)
      - price_stats  : mean, median, min, max, p25 (recommended sell price)
      - vinted_query : search query used
      - items        : 50 Vinted results
    """
    query = request.form.get("query", "").strip() or None

    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "webp": "image/webp",
    }
    images = []
    for file in request.files.getlist("images"):
        if file and file.filename and _allowed_file(file.filename):
            ext = file.filename.rsplit(".", 1)[1].lower()
            images.append((file.read(), mime_map.get(ext, "image/jpeg")))

    if not images and not query:
        return jsonify({"error": "Fournissez une image et/ou une description."}), 400

    # --- Prizzy IA analysis ---
    try:
        analysis = analyze_clothing(images, query)
    except Exception as exc:
        return jsonify({"error": f"Erreur d'analyse Prizzy IA : {exc}"}), 502

    # Build search query: prefer Prizzy IA's optimized query, fallback to raw input
    vinted_query = (analysis.get("query_vinted") or query or "").strip()
    if not vinted_query:
        return jsonify({"error": "Impossible de construire une requête de recherche."}), 400

    # --- Vinted search (50 items) ---
    try:
        vinted_results = search_items(vinted_query, per_page=50, page=1)
    except Exception as exc:
        return jsonify({"error": f"Erreur Vinted : {exc}"}), 502

    # --- Price statistics ---
    prices = [
        float(item["price_amount"])
        for item in vinted_results["items"]
        if item.get("price_amount") is not None
    ]

    price_stats = {}
    if prices:
        sorted_p = sorted(prices)
        n = len(sorted_p)
        q1 = statistics.quantiles(sorted_p, n=4)[0] if n >= 4 else sorted_p[0]
        price_stats = {
            "count": n,
            "mean": round(statistics.mean(sorted_p), 2),
            "median": round(statistics.median(sorted_p), 2),
            "min": round(sorted_p[0], 2),
            "max": round(sorted_p[-1], 2),
            "recommended_price_7j": round(q1, 2),
        }

    total_entries = vinted_results.get("total_entries", 0)
    return jsonify({
        "analysis": analysis,
        "price_stats": price_stats,
        "sale_score": _compute_sale_score(price_stats, total_entries),
        "publish_time": _best_publish_time(price_stats),
        "vinted_query": vinted_query,
        "total_entries": total_entries,
        "items": vinted_results["items"],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
