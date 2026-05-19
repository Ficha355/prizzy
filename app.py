from dotenv import load_dotenv
load_dotenv()

import os as _os
# Map FAL_API_KEY → FAL_KEY read by fal_client
_fal_k = _os.environ.get("FAL_API_KEY", "").strip()
if _fal_k:
    _os.environ.setdefault("FAL_KEY", _fal_k)

import stripe
from flask import (Flask, request, jsonify, render_template,
                   send_from_directory, session, redirect, make_response)
from werkzeug.utils import secure_filename
import os
import uuid
import statistics

import base64
import io
from PIL import Image

from vinted_client import search_items, get_seller_items
from claude_client import analyze_clothing, legit_check, audit_seller_profile
import bcrypt
from db import init_db, upsert_subscriber, has_active_subscription, has_elite_subscription, has_premium_subscription, get_subscriber, count_active_subscribers, increment_analyses_count, get_analyses_count, set_analyses_count, set_password, get_password_hash, increment_user_analyses, get_user_analyses

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

@app.route("/stats")
def stats():
    return jsonify({
        "subscribers": count_active_subscribers(),
        "analyses":    get_analyses_count(),
    })


@app.route("/me")
def me():
    email = session.get("email")
    if not email:
        return jsonify({"error": "Non connecté"}), 401
    sub = get_subscriber(email)
    if not sub or sub["status"] != "active":
        return jsonify({"error": "Pas d'abonnement actif"}), 403
    plan = sub.get("plan", "starter")
    print(f"[/me] email={email} plan={plan}", flush=True)
    return jsonify({"email": email, "plan": plan})


# ── Admin ─────────────────────────────────────────────────

@app.route("/admin/add-elite", methods=["POST"])
def admin_add_elite():
    token = request.headers.get("X-Admin-Token") or request.form.get("token")
    expected = os.environ.get("ADMIN_TOKEN", "")
    if not expected or token != expected:
        return jsonify({"error": "Non autorisé"}), 401

    action = request.form.get("action", "").strip()

    if action == "set_analyses":
        try:
            n = int(request.form.get("count", 0))
        except ValueError:
            return jsonify({"error": "Paramètre 'count' invalide"}), 400
        set_analyses_count(n)
        return jsonify({"ok": True, "analyses": n})

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Paramètre 'email' manquant"}), 400

    plan = (request.form.get("plan") or "elite").strip().lower()
    if plan not in ("elite", "ultimate", "starter"):
        plan = "elite"
    upsert_subscriber(email=email, status="active", plan=plan)
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

def _ref_cookie():
    return request.cookies.get("ref", "").strip()[:64]


@app.route("/")
def index():
    ref = request.args.get("ref", "").strip()[:64]
    email = session.get("email")
    sub = get_subscriber(email) if email else None
    if sub and sub.get("status") == "active":
        plan = sub.get("plan", "starter")
        resp = make_response(render_template("index.html", email=email, plan=plan))
    else:
        resp = make_response(render_template("landing.html"))
    if ref:
        resp.set_cookie("ref", ref, max_age=30 * 24 * 3600, httponly=True, samesite="Lax")
    return resp


# ── Stripe subscription ───────────────────────────────────

_LOGIN_MSG = "Connectez-vous ou créez un compte pour vous abonner."


@app.route("/subscribe", methods=["POST"])
def subscribe():
    email = session.get("email")
    if not email:
        return redirect(f"/login?info={_LOGIN_MSG}")
    try:
        meta = {"plan": "starter"}
        if _ref_cookie():
            meta["ref"] = _ref_cookie()
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": os.environ.get("STRIPE_PRICE_ID"), "quantity": 1}],
            mode="subscription",
            allow_promotion_codes=True,
            subscription_data={"trial_period_days": 7},
            customer_email=email,
            metadata=meta,
            success_url=request.url_root + "success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.url_root + "cancel",
            locale="fr",
        )
        return redirect(checkout.url, code=303)
    except Exception as exc:
        return render_template("landing.html", error=str(exc))


@app.route("/subscribe-ultimate", methods=["POST"])
def subscribe_ultimate():
    email = session.get("email")
    if not email:
        return redirect(f"/login?info={_LOGIN_MSG}")
    try:
        meta = {"plan": "ultimate"}
        if _ref_cookie():
            meta["ref"] = _ref_cookie()
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": os.environ.get("STRIPE_PRICE_ID_ULTIMATE"), "quantity": 1}],
            mode="subscription",
            allow_promotion_codes=True,
            subscription_data={"trial_period_days": 7},
            customer_email=email,
            metadata=meta,
            success_url=request.url_root + "success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.url_root + "cancel",
            locale="fr",
        )
        return redirect(checkout.url, code=303)
    except Exception as exc:
        return render_template("landing.html", error=str(exc))


@app.route("/subscribe-elite", methods=["POST"])
def subscribe_elite():
    email = session.get("email")
    if not email:
        return redirect(f"/login?info={_LOGIN_MSG}")
    try:
        meta = {"plan": "elite"}
        if _ref_cookie():
            meta["ref"] = _ref_cookie()
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": os.environ.get("STRIPE_PRICE_ID_ELITE"), "quantity": 1}],
            mode="subscription",
            allow_promotion_codes=True,
            subscription_data={"trial_period_days": 7},
            customer_email=email,
            metadata=meta,
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
        sub_status = "active"
        if checkout.subscription:
            sub = stripe.Subscription.retrieve(checkout.subscription)
            if sub.status == "trialing":
                sub_status = "trialing"
        print(f"[success] email={email} plan={plan} status={sub_status}", flush=True)
        upsert_subscriber(
            email=email,
            stripe_customer_id=checkout.customer,
            stripe_subscription_id=checkout.subscription,
            status=sub_status,
            plan=plan,
        )
        session["email"] = email
    except Exception:
        pass
    return redirect("/")


@app.route("/cancel")
def cancel():
    return render_template("cancel.html")


@app.route("/demo")
def demo():
    return render_template("demo.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        info = request.args.get("info", "")
        return render_template("login.html", login_info=info)
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email:
        return render_template("login.html", login_error="Entrez votre adresse email.")
    if not has_active_subscription(email):
        return render_template("login.html", login_error="Aucun abonnement actif pour cet email.")
    stored_hash = get_password_hash(email)
    if stored_hash is None:
        return redirect(f"/set-password?email={email}")
    if not bcrypt.checkpw(password.encode(), stored_hash.encode()):
        return render_template("login.html", login_error="Mot de passe incorrect.")
    session["email"] = email
    return redirect("/")


@app.route("/set-password", methods=["GET", "POST"])
def set_password_route():
    email = request.args.get("email", "").strip().lower() or request.form.get("email", "").strip().lower()
    if not email or not has_active_subscription(email):
        return redirect("/")
    if request.method == "GET":
        return render_template("set_password.html", email=email)
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    if len(password) < 8:
        return render_template("set_password.html", email=email, error="Le mot de passe doit faire au moins 8 caractères.")
    if password != confirm:
        return render_template("set_password.html", email=email, error="Les mots de passe ne correspondent pas.")
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    set_password(email, hashed)
    session["email"] = email
    return redirect("/")


@app.route("/logout")
def logout():
    session.pop("email", None)
    return redirect("/")


@app.route("/account")
def account():
    email = session.get("email")
    if not email:
        return redirect("/login")
    sub = get_subscriber(email)
    if not sub or sub.get("status") not in ("active", "trialing"):
        return redirect("/")

    plan_labels = {"starter": "Starter", "elite": "Elite", "ultimate": "Ultimate"}
    plan = plan_labels.get(sub.get("plan", "starter"), "Starter")

    trial_end = None
    next_renewal = None
    stripe_sub_id = sub.get("stripe_subscription_id")
    if stripe_sub_id:
        try:
            import datetime
            stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
            if stripe_sub.get("trial_end"):
                trial_end = datetime.datetime.fromtimestamp(stripe_sub["trial_end"]).strftime("%-d %B %Y")
            if stripe_sub.get("current_period_end"):
                next_renewal = datetime.datetime.fromtimestamp(stripe_sub["current_period_end"]).strftime("%-d %B %Y")
        except Exception:
            pass

    return render_template("account.html",
                           email=email,
                           plan=plan,
                           status=sub.get("status"),
                           trial_end=trial_end,
                           next_renewal=next_renewal,
                           has_portal=bool(sub.get("stripe_customer_id")),
                           analyses=get_user_analyses(email))


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


@app.route("/profile-audit", methods=["GET"])
def profile_audit():
    email = session.get("email")
    if not email or not has_active_subscription(email):
        return redirect("/login?info=Connectez-vous pour accéder à l'audit de profil.")
    return render_template("profile_audit.html", email=email)


@app.route("/profile-audit/checkout", methods=["POST"])
def profile_audit_checkout():
    email = session.get("email")
    if not email or not has_active_subscription(email):
        return redirect("/login?info=Connectez-vous pour accéder à l'audit de profil Vinted.")
    profile_url = request.form.get("profile_url", "").strip()
    if not profile_url or "vinted" not in profile_url.lower():
        return render_template("profile_audit.html", email=email,
                               form_error="Entrez une URL de profil Vinted valide.")
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "unit_amount": 99,
                    "product_data": {
                        "name": "Audit de profil Vinted — Prizzy",
                        "description": "Analyse IA de vos annonces : prix, stratégie, actions prioritaires",
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=email,
            metadata={"profile_url": profile_url[:400], "email": email},
            success_url=request.url_root + "profile-audit/result?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.url_root + "profile-audit",
            locale="fr",
        )
        return redirect(checkout.url, code=303)
    except Exception as exc:
        return render_template("profile_audit.html", email=email,
                               form_error=f"Erreur paiement : {exc}")


@app.route("/profile-audit/result", methods=["GET"])
def profile_audit_result():
    email = session.get("email")
    if not email:
        return redirect("/login")
    session_id = request.args.get("session_id", "")
    if not session_id:
        return redirect("/profile-audit")
    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return render_template("profile_audit.html", email=email,
                               result_error="Session de paiement introuvable.")
    if checkout.payment_status != "paid":
        return render_template("profile_audit.html", email=email,
                               result_error="Paiement non finalisé.")
    profile_url = checkout.metadata.get("profile_url", "")
    if not profile_url:
        return render_template("profile_audit.html", email=email,
                               result_error="URL de profil manquante.")
    try:
        user_info, items = get_seller_items(profile_url, max_items=50)
    except ValueError as exc:
        return render_template("profile_audit.html", email=email,
                               result_error=str(exc))
    except Exception as exc:
        return render_template("profile_audit.html", email=email,
                               result_error=f"Impossible de récupérer les annonces : {exc}")
    if not items:
        return render_template("profile_audit.html", email=email,
                               result_error="Aucune annonce active trouvée sur ce profil.")
    try:
        audit = audit_seller_profile(user_info, items)
    except Exception as exc:
        return render_template("profile_audit.html", email=email,
                               result_error=f"Erreur analyse IA : {exc}")
    return render_template("profile_audit.html", email=email,
                           user_info=user_info, items=items, audit=audit,
                           profile_url=profile_url)


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except ValueError as e:
        app.logger.error("[webhook] invalid payload: %s", e)
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError as e:
        app.logger.error("[webhook] invalid signature: %s", e)
        return jsonify({"error": "Invalid signature"}), 400

    etype = event["type"]
    obj   = event["data"]["object"]
    app.logger.info(
        "[webhook] received event=%s id=%s customer=%s status=%s",
        etype, event.get("id"), obj.get("customer"), obj.get("status"),
    )

    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        sub_status = obj.get("status")
        customer_id = obj["customer"]
        sub_id = obj["id"]
        is_new = etype == "customer.subscription.created"
        app.logger.info(
            "[webhook] subscription event: sub_id=%s customer=%s status=%s new=%s",
            sub_id, customer_id, sub_status, is_new,
        )
        if sub_status in ("canceled", "unpaid", "past_due", "incomplete_expired"):
            _upsert_customer(customer_id, sub_id, "inactive", plan=None)
        elif sub_status == "trialing":
            plan = _plan_from_subscription(obj)
            _upsert_customer(customer_id, sub_id, "trialing", plan=plan)
            if is_new:
                _send_welcome_email(customer_id, plan)
        elif sub_status == "active":
            plan = _plan_from_subscription(obj)
            _upsert_customer(customer_id, sub_id, "active", plan=plan)
            if is_new:
                _send_welcome_email(customer_id, plan)
        else:
            app.logger.warning("[webhook] unhandled subscription status=%s", sub_status)

    elif etype == "customer.subscription.deleted":
        app.logger.info("[webhook] subscription deleted customer=%s", obj["customer"])
        _upsert_customer(obj["customer"], obj["id"], "inactive", plan=None)

    elif etype == "customer.subscription.trial_will_end":
        # Fired 3 days before trial ends — log only, no DB change needed
        app.logger.info(
            "[webhook] trial_will_end customer=%s trial_end=%s",
            obj["customer"], obj.get("trial_end"),
        )

    elif etype == "invoice.payment_succeeded":
        # Fires when a trial converts to paid or a renewal succeeds
        customer_id = obj.get("customer")
        sub_id = obj.get("subscription")
        app.logger.info(
            "[webhook] payment_succeeded customer=%s sub=%s amount=%s",
            customer_id, sub_id, obj.get("amount_paid"),
        )
        if sub_id:
            try:
                sub = stripe.Subscription.retrieve(sub_id)
                plan = _plan_from_subscription(sub)
                _upsert_customer(customer_id, sub_id, "active", plan=plan)
            except Exception as e:
                app.logger.error("[webhook] invoice.payment_succeeded sub retrieve error: %s", e)

    elif etype == "invoice.payment_failed":
        app.logger.warning("[webhook] payment_failed customer=%s", obj.get("customer"))
        _upsert_customer(obj["customer"], obj.get("subscription"), "inactive", plan=None)

    else:
        app.logger.info("[webhook] ignored event type=%s", etype)

    return jsonify({"status": "ok"})


def _plan_from_subscription(sub_obj) -> Optional[str]:
    """Detect plan name from the price ID in subscription items."""
    price_map = {
        os.environ.get("STRIPE_PRICE_ID", ""):          "starter",
        os.environ.get("STRIPE_PRICE_ID_ELITE", ""):    "elite",
        os.environ.get("STRIPE_PRICE_ID_ULTIMATE", ""): "ultimate",
    }
    price_map.pop("", None)  # remove empty-key entries
    try:
        items = sub_obj.get("items", {}).get("data", [])
        for item in items:
            price_id = item.get("price", {}).get("id", "")
            if price_id in price_map:
                plan = price_map[price_id]
                app.logger.info("[webhook] plan detected: price_id=%s → %s", price_id, plan)
                return plan
        app.logger.warning(
            "[webhook] unknown price_ids in subscription: %s",
            [item.get("price", {}).get("id") for item in items],
        )
    except Exception as e:
        app.logger.error("[webhook] _plan_from_subscription error: %s", e)
    return None


def _upsert_customer(customer_id: str, sub_id: Optional[str], status: str, plan: Optional[str]):
    try:
        c = stripe.Customer.retrieve(customer_id)
        email = c.email
        if not email:
            app.logger.error("[webhook] customer %s has no email", customer_id)
            return
        app.logger.info(
            "[webhook] upsert email=%s customer=%s sub=%s status=%s plan=%s",
            email, customer_id, sub_id, status, plan,
        )
        upsert_subscriber(
            email=email,
            stripe_customer_id=customer_id,
            stripe_subscription_id=sub_id,
            status=status,
            plan=plan,
        )
    except Exception as e:
        app.logger.error("[webhook] _upsert_customer error customer=%s: %s", customer_id, e)


def _activate_customer(customer_id: str):
    _upsert_customer(customer_id, None, "active", plan=None)


def _deactivate_customer(customer_id: str):
    _upsert_customer(customer_id, None, "inactive", plan=None)


def _send_welcome_email(customer_id: str, plan: Optional[str]):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    mail_user = os.environ.get("MAIL_USERNAME", "").strip()
    mail_pass = os.environ.get("MAIL_PASSWORD", "").strip()
    if not mail_user or not mail_pass:
        app.logger.warning("[welcome-mail] MAIL_USERNAME ou MAIL_PASSWORD manquant, email non envoyé")
        return

    try:
        c = stripe.Customer.retrieve(customer_id)
        to_email = c.email
        if not to_email:
            app.logger.error("[welcome-mail] customer %s sans email", customer_id)
            return
    except Exception as e:
        app.logger.error("[welcome-mail] impossible de récupérer le customer %s: %s", customer_id, e)
        return

    plan_labels = {"starter": "Starter", "elite": "Elite", "ultimate": "Ultimate"}
    plan_label = plan_labels.get(plan or "", "Starter")

    subject = f"Bienvenue sur Prizzy {plan_label} 🎉"
    html = f"""\
<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#F7F8F9;font-family:Inter,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#F7F8F9;padding:40px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:16px;border:1px solid #E5E7EB;padding:40px 36px;">
        <tr><td>
          <p style="margin:0 0 4px;font-size:28px;font-weight:700;color:#09B1BA;letter-spacing:-0.5px;">
            Prizzy
          </p>
          <p style="margin:0 0 28px;font-size:13px;color:#6B7280;">
            Ton assistant IA pour vendre mieux sur Vinted
          </p>
          <h1 style="margin:0 0 12px;font-size:22px;font-weight:700;color:#111827;">
            Bienvenue sur le plan <span style="color:#09B1BA;">{plan_label}</span>&nbsp;!
          </h1>
          <p style="margin:0 0 24px;font-size:15px;color:#374151;line-height:1.6;">
            Ton abonnement est activé. Pour commencer, connecte-toi à Prizzy et
            <strong>crée ton mot de passe</strong> lors de ta première connexion.
          </p>

          <table cellpadding="0" cellspacing="0" style="margin:0 0 28px;">
            <tr><td style="background:#F0FDFD;border-left:4px solid #09B1BA;
                           border-radius:8px;padding:16px 20px;">
              <p style="margin:0 0 6px;font-size:13px;font-weight:600;color:#09B1BA;
                         text-transform:uppercase;letter-spacing:.5px;">
                Comment créer ton mot de passe
              </p>
              <ol style="margin:0;padding-left:18px;font-size:14px;color:#374151;line-height:1.8;">
                <li>Clique sur le bouton ci-dessous</li>
                <li>Entre ton adresse email (<strong>{to_email}</strong>)</li>
                <li>Laisse le champ mot de passe <strong>vide</strong> et valide</li>
                <li>Tu seras redirigé vers la page de création de mot de passe</li>
              </ol>
            </td></tr>
          </table>

          <table cellpadding="0" cellspacing="0" style="margin:0 0 32px;">
            <tr><td align="center"
                    style="background:#09B1BA;border-radius:10px;padding:14px 32px;">
              <a href="https://prizzy.onrender.com/login"
                 style="color:#fff;font-size:16px;font-weight:600;text-decoration:none;">
                Se connecter à Prizzy →
              </a>
            </td></tr>
          </table>

          <p style="margin:0;font-size:13px;color:#9CA3AF;line-height:1.5;">
            Une question ? Réponds directement à cet email.<br>
            L'équipe Prizzy
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Prizzy <{mail_user}>"
    msg["To"]      = to_email
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(mail_user, mail_pass)
            smtp.sendmail(mail_user, to_email, msg.as_string())
        app.logger.info("[welcome-mail] envoyé à %s (plan=%s)", to_email, plan_label)
    except Exception as e:
        app.logger.error("[welcome-mail] échec envoi à %s: %s", to_email, e)


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

    increment_user_analyses(email)
    return jsonify(result)


# ── Uploads (serve saved generated images) ───────────────

@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── Photo IA (Elite) ──────────────────────────────────────

@app.route("/photo-ia", methods=["POST"])
def photo_ia():
    bot_token   = request.headers.get("X-Bot-Token", "")
    admin_token = os.environ.get("ADMIN_TOKEN", "")
    email = None
    if not (bot_token and admin_token and bot_token == admin_token):
        email = session.get("email")
        if not email or not has_premium_subscription(email):
            return jsonify({"error": "Plan Prizzy Elite ou Ultimate requis.", "redirect": "/"}), 403

    mode = request.form.get("mode", "plie")  # "plie" | "aplat"

    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png",  "webp": "image/webp"}
    file = request.files.get("image")
    if not file or not file.filename or not _allowed_file(file.filename):
        return jsonify({"error": "Fournissez une image valide."}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    image_data, _ = _compress_image(file.read(), mime_map.get(ext, "image/jpeg"))

    png_buf = io.BytesIO()
    Image.open(io.BytesIO(image_data)).convert("RGBA").save(png_buf, format="PNG")
    png_buf.seek(0)
    png_buf.name = "image.png"

    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not openai_key:
        return jsonify({"error": "Clé OpenAI manquante (OPENAI_API_KEY)."}), 500

    import openai as _openai
    oa = _openai.OpenAI(api_key=openai_key)

    # Step 1: gpt-4o-mini vision → precise garment description
    try:
        vision_b64 = base64.b64encode(image_data).decode()
        vision_mime = mime_map.get(ext, "image/jpeg")
        vision_resp = oa.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{vision_mime};base64,{vision_b64}"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe this garment precisely for an image editing prompt. "
                            "Include: garment type, exact color and any color variations, "
                            "fabric texture, visible patterns, logos, labels, text, or embroidery, "
                            "and the current background. Be concise but exhaustive."
                        ),
                    },
                ],
            }],
            max_tokens=300,
        )
        garment_desc = vision_resp.choices[0].message.content.strip()
        app.logger.info("[photo-ia] vision description: %s", garment_desc)
    except Exception as exc:
        app.logger.error("[photo-ia] vision error: %s", exc, exc_info=True)
        return jsonify({"error": f"Erreur vision GPT-4o-mini : {exc}"}), 502

    # Step 2: gpt-image-1 edit with the enriched prompt
    if mode == "aplat":
        prompt = (
            f"Lay this garment completely flat on a surface. "
            f"The garment is: {garment_desc}. "
            f"Do not change anything else — not the color, not the background, not any text, logo, label or embroidery. "
            f"Only lay it flat and smooth. "
            f"Preserve the exact original color."
        )
    else:
        prompt = (
            f"Fold this garment neatly and lay it flat on the surface. "
            f"The garment is: {garment_desc}. "
            f"Do not change anything else — not the color, not the background, not any text, logo, label or embroidery. "
            f"Only fold it. "
            f"Preserve the exact original color of the garment — do not lighten, darken, or change the saturation in any way."
        )
    app.logger.info("[photo-ia] sending to gpt-image-1 (%d bytes PNG)", png_buf.getbuffer().nbytes)

    try:
        edit_resp = oa.images.edit(
            model="gpt-image-1",
            image=png_buf,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
        b64_data = edit_resp.data[0].b64_json
    except Exception as exc:
        app.logger.error("[photo-ia] gpt-image-1 error: %s", exc, exc_info=True)
        return jsonify({"error": f"Erreur gpt-image-1 : {exc}"}), 502

    try:
        filename  = f"photo_ia_{uuid.uuid4().hex}.png"
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(save_path, "wb") as f:
            f.write(base64.b64decode(b64_data))
        local_url = f"/uploads/{filename}"
    except Exception as exc:
        app.logger.error("[photo-ia] save error: %s", exc)
        return jsonify({"error": "Erreur lors de la sauvegarde de l'image."}), 500

    increment_analyses_count()
    if email:
        increment_user_analyses(email)
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
    increment_analyses_count()
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
    increment_analyses_count()
    increment_user_analyses(email)
    return jsonify({
        "analysis":     analysis,
        "price_stats":  price_stats,
        "sale_score":   _compute_sale_score(price_stats, total_entries),
        "publish_time": _best_publish_time(price_stats),
        "vinted_query": vinted_query,
        "total_entries": total_entries,
        "items":        vinted_results["items"],
    })


@app.route("/mentions-legales")
def mentions_legales():
    return render_template("mentions-legales.html")


@app.route("/cgv")
def cgv():
    return render_template("cgv.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
