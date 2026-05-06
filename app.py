from dotenv import load_dotenv
load_dotenv()

import stripe
from flask import (Flask, request, jsonify, render_template,
                   send_from_directory, session, redirect)
from werkzeug.utils import secure_filename
import os
import uuid
import statistics

import base64
import io
from PIL import Image

from vinted_client import search_items
from claude_client import analyze_clothing, legit_check
from db import init_db, upsert_subscriber, has_active_subscription, has_elite_subscription, has_premium_subscription, get_subscriber

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB (up to 5 photos)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

stripe.api_key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()

init_db()


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


_SIZE_LIMIT = 5 * 1024 * 1024  # 5 MB


def _compress_image(data: bytes, mime_type: str) -> tuple[bytes, str]:
    if len(data) <= _SIZE_LIMIT:
        return data, mime_type
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((1920, 1920), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue(), "image/jpeg"


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

    if total_entries < 30:      comp = 4
    elif total_entries < 100:   comp = 3
    elif total_entries < 300:   comp = 2
    elif total_entries < 800:   comp = 1
    else:                       comp = 0

    ratio = rec / mean if mean > 0 else 1.0
    if ratio < 0.55:    price = 3
    elif ratio < 0.70:  price = 2
    elif ratio < 0.85:  price = 1
    else:               price = 0

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
    if rec > 50:
        return {"jour": "Jeudi", "creneau": "19h – 21h"}
    elif rec > 20:
        return {"jour": "Dimanche", "creneau": "18h – 20h"}
    else:
        return {"jour": "Samedi", "creneau": "10h – 12h"}


# ── Health check ─────────────────────────────────────────

@app.route("/ping")
def ping():
    return "pong"


# ── Me ────────────────────────────────────────────────────

@app.route("/me")
def me():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Non connecté"}), 401
    sub = get_subscriber(email)
    if not sub or sub["status"] != "active":
        return jsonify({"error": "Pas d'abonnement actif"}), 403
    return jsonify({"email": email, "plan": sub.get("plan", "starter")})


# ── Admin ─────────────────────────────────────────────────

@app.route("/admin/add-elite", methods=["POST"])
def admin_add_elite():
    token = request.headers.get("X-Admin-Token") or request.form.get("token")
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected or token != expected:
        return jsonify({"error": "Non autorisé"}), 401

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Paramètre 'email' manquant"}), 400

    upsert_subscriber(email=email, status="active", plan="elite")
    sub = get_subscriber(email)
    return jsonify({"ok": True, "subscriber": dict(sub)})


# ── Static / PWA ──────────────────────────────────────────

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


# ── Main app (gated) ──────────────────────────────────────

@app.route("/")
def index():
    email = session.get("email")
    if email and has_active_subscription(email):
        return render_template("index.html", email=email)
    return render_template("landing.html")


# ── Stripe subscription ───────────────────────────────────

@app.route("/subscribe", methods=["POST"])
def subscribe():
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": os.environ.get("STRIPE_PRICE_ID"), "quantity": 1}],
            mode="subscription",
            metadata={"plan": "starter"},
            success_url=request.url_root + "success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.url_root + "cancel",
            locale="fr",
        )
        return redirect(checkout.url, code=303)
    except Exception as exc:
        return render_template("landing.html", error=str(exc))


@app.route("/subscribe-ultimate", methods=["POST"])
def subscribe_ultimate():
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": os.environ.get("STRIPE_PRICE_ID_ULTIMATE"), "quantity": 1}],
            mode="subscription",
            metadata={"plan": "ultimate"},
            success_url=request.url_root + "success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.url_root + "cancel",
            locale="fr",
        )
        return redirect(checkout.url, code=303)
    except Exception as exc:
        return render_template("landing.html", error=str(exc))


@app.route("/subscribe-elite", methods=["POST"])
def subscribe_elite():
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": os.environ.get("STRIPE_PRICE_ID_ELITE"), "quantity": 1}],
            mode="subscription",
            metadata={"plan": "elite"},
            success_url=request.url_root + "success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.url_root + "cancel",
            locale="fr",
        )
        return redirect(checkout.url, code=303)
    except Exception as exc:
        return render_template("landing.html", error=str(exc))


@app.route("/success")
def success():
    session_id = request.args.get("session_id")
    if not session_id:
        return redirect("/")
    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
        email = checkout.customer_details.email
        plan = checkout.metadata.get("plan", "starter")
        upsert_subscriber(
            email=email,
            stripe_customer_id=checkout.customer,
            stripe_subscription_id=checkout.subscription,
            status="active",
            plan=plan,
        )
        session["email"] = email
    except Exception:
        pass
    return redirect("/")


@app.route("/cancel")
def cancel():
    return render_template("cancel.html")


@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip().lower()
    if not email:
        return render_template("landing.html", login_error="Entrez votre adresse email.")
    if has_active_subscription(email):
        session["email"] = email
        return redirect("/")
    return render_template("landing.html", login_error="Aucun abonnement actif pour cet email.")


@app.route("/logout")
def logout():
    session.pop("email", None)
    return redirect("/")


@app.route("/portal", methods=["POST"])
def portal():
    email = session.get("email")
    sub = get_subscriber(email) if email else None
    if not sub or not sub.get("stripe_customer_id"):
        return redirect("/")
    portal_session = stripe.billing_portal.Session.create(
        customer=sub["stripe_customer_id"],
        return_url=request.url_root,
    )
    return redirect(portal_session.url, code=303)


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid signature"}), 400

    etype = event["type"]
    obj   = event["data"]["object"]

    if etype == "customer.subscription.deleted":
        _deactivate_customer(obj["customer"])
    elif etype == "customer.subscription.updated":
        if obj["status"] in ("canceled", "unpaid", "past_due"):
            _deactivate_customer(obj["customer"])
        elif obj["status"] == "active":
            _activate_customer(obj["customer"])
    elif etype == "invoice.payment_failed":
        _deactivate_customer(obj["customer"])

    return jsonify({"status": "ok"})


def _activate_customer(customer_id: str):
    try:
        c = stripe.Customer.retrieve(customer_id)
        if c.email:
            upsert_subscriber(c.email, status="active")
    except Exception:
        pass


def _deactivate_customer(customer_id: str):
    try:
        c = stripe.Customer.retrieve(customer_id)
        if c.email:
            upsert_subscriber(c.email, status="inactive")
    except Exception:
        pass


# ── Legit Check (Elite) ───────────────────────────────────

@app.route("/legit-check", methods=["POST"])
def legit_check_route():
    email = session.get("email")
    if not email or not has_premium_subscription(email):
        return jsonify({"error": "Plan Prizzy Elite ou Ultimate requis.", "redirect": "/"}), 403

    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png",  "webp": "image/webp",
    }
    images = []
    for file in request.files.getlist("images"):
        if file and file.filename and _allowed_file(file.filename):
            ext = file.filename.rsplit(".", 1)[1].lower()
            data, mime = _compress_image(file.read(), mime_map.get(ext, "image/jpeg"))
            images.append((data, mime))

    if not images:
        return jsonify({"error": "Fournissez au moins une image."}), 400

    try:
        result = legit_check(images)
    except Exception as exc:
        return jsonify({"error": f"Erreur Legit Check IA : {exc}"}), 502

    return jsonify(result)


# ── Uploads (serve saved generated images) ───────────────

@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── Photo IA (Elite) ──────────────────────────────────────

@app.route("/photo-ia", methods=["POST"])
def photo_ia():
    email = session.get("email")
    if not email or not has_premium_subscription(email):
        return jsonify({"error": "Plan Prizzy Elite ou Ultimate requis.", "redirect": "/"}), 403

    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png",  "webp": "image/webp",
    }

    file = request.files.get("image")
    if not file or not file.filename or not _allowed_file(file.filename):
        return jsonify({"error": "Fournissez une image valide."}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    image_data, _ = _compress_image(file.read(), mime_map.get(ext, "image/jpeg"))

    # Convert to PNG (required by the images.edit endpoint)
    png_buf = io.BytesIO()
    Image.open(io.BytesIO(image_data)).convert("RGBA").save(png_buf, format="PNG")
    png_buf.seek(0)
    png_buf.name = "image.png"

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not openai_key:
        return jsonify({"error": "Clé OpenAI manquante (OPENAI_API_KEY)."}), 500

    prompt = (
        "You are retouching a product photo of a garment for an e-commerce listing. "
        "Keep the original background exactly as it is — never replace it with white or any other background. "
        "Only clean it up: remove stray objects, feet, hands, or any distracting elements that are not the garment. "
        "Apply these improvements: straighten and neatly fold or lay flat the garment, fix any creases, "
        "improve lighting to be uniform and well-exposed, reframe and center the garment properly, "
        "sharp focus, high resolution. "
        "Keep the garment identical — same color, fabric, brand markings, and style."
    )
    app.logger.info("[photo-ia] Sending to gpt-image-1 edit (%d bytes PNG), prompt: %s",
                    png_buf.getbuffer().nbytes, prompt)

    try:
        import openai as _openai
        oa = _openai.OpenAI(api_key=openai_key)
        edit_resp = oa.images.edit(
            model="gpt-image-1",
            image=png_buf,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
        b64_data = edit_resp.data[0].b64_json
        app.logger.info("[photo-ia] gpt-image-1 response received")
    except Exception as exc:
        app.logger.error("[photo-ia] gpt-image-1 error (type=%s): %s",
                         type(exc).__name__, exc, exc_info=True)
        return jsonify({"error": f"Erreur GPT-4o image : {exc}"}), 502

    try:
        filename = f"photo_ia_{uuid.uuid4().hex}.png"
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(save_path, "wb") as f:
            f.write(base64.b64decode(b64_data))
        local_url = f"/uploads/{filename}"
    except Exception as exc:
        app.logger.error("[photo-ia] save error: %s", exc)
        return jsonify({"error": "Erreur lors de la sauvegarde de l'image."}), 500

    return jsonify({"image_url": local_url})


# ── Search ────────────────────────────────────────────────

@app.route("/search", methods=["POST"])
def search():
    query = request.form.get("query", "").strip()
    if not query:
        return jsonify({"error": "Le champ 'query' est obligatoire."}), 400

    page     = int(request.form.get("page", 1))
    per_page = min(int(request.form.get("per_page", 20)), 100)

    image_path = None
    if "image" in request.files:
        file = request.files["image"]
        if file and file.filename and _allowed_file(file.filename):
            ext      = secure_filename(file.filename).rsplit(".", 1)[1].lower()
            filename = f"{uuid.uuid4().hex}.{ext}"
            image_path = os.path.join(UPLOAD_FOLDER, filename)
            file.save(image_path)

    try:
        results = search_items(query, per_page=per_page, page=page)
    except Exception as exc:
        return jsonify({"error": f"Erreur lors de la recherche Vinted : {exc}"}), 502

    results["query"]          = query
    results["image_uploaded"] = image_path is not None
    return jsonify(results)


# ── Analyze (subscription required) ──────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    email = session.get("email")
    if not email or not has_active_subscription(email):
        return jsonify({"error": "Abonnement requis.", "redirect": "/"}), 403

    query = request.form.get("query", "").strip() or None

    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png",  "webp": "image/webp",
    }
    images = []
    for file in request.files.getlist("images"):
        if file and file.filename and _allowed_file(file.filename):
            ext = file.filename.rsplit(".", 1)[1].lower()
            data, mime = _compress_image(file.read(), mime_map.get(ext, "image/jpeg"))
            images.append((data, mime))

    if not images and not query:
        return jsonify({"error": "Fournissez une image et/ou une description."}), 400

    try:
        analysis = analyze_clothing(images, query)
    except Exception as exc:
        return jsonify({"error": f"Erreur d'analyse Prizzy IA : {exc}"}), 502

    vinted_query = (analysis.get("query_vinted") or query or "").strip()
    if not vinted_query:
        return jsonify({"error": "Impossible de construire une requête de recherche."}), 400

    try:
        vinted_results = search_items(vinted_query, per_page=50, page=1)
    except Exception as exc:
        return jsonify({"error": f"Erreur Vinted : {exc}"}), 502

    prices = [
        float(item["price_amount"])
        for item in vinted_results["items"]
        if item.get("price_amount") is not None
    ]

    price_stats = {}
    if prices:
        sorted_p = sorted(prices)
        n  = len(sorted_p)
        q1 = statistics.quantiles(sorted_p, n=4)[0] if n >= 4 else sorted_p[0]
        price_stats = {
            "count":               n,
            "mean":                round(statistics.mean(sorted_p), 2),
            "median":              round(statistics.median(sorted_p), 2),
            "min":                 round(sorted_p[0], 2),
            "max":                 round(sorted_p[-1], 2),
            "recommended_price_7j": round(q1, 2),
        }

    total_entries = vinted_results.get("total_entries", 0)
    return jsonify({
        "analysis":     analysis,
        "price_stats":  price_stats,
        "sale_score":   _compute_sale_score(price_stats, total_entries),
        "publish_time": _best_publish_time(price_stats),
        "vinted_query": vinted_query,
        "total_entries": total_entries,
        "items":        vinted_results["items"],
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
