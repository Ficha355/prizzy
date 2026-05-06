import base64
import json
import re
from typing import Optional

import anthropic

MODEL = "claude-sonnet-4-20250514"

_client = anthropic.Anthropic()

_SYSTEM = (
    "Tu es un expert en mode et en vente de vêtements d'occasion sur Vinted. "
    "Réponds UNIQUEMENT avec un objet JSON valide, sans texte supplémentaire, "
    "sans bloc markdown, sans explication."
)

_SCHEMA = """\
Retourne un JSON avec exactement ces clés :
{
  "marque": "marque du vêtement (string ou null si inconnue)",
  "type_vetement": "type précis ex: veste en jean, t-shirt oversize, manteau laine (string)",
  "couleur": "couleur principale (string ou null)",
  "taille": "taille ex: S, M, L, XL, 38, 40... (string ou null)",
  "etat_estime": "un parmi : Neuf avec étiquette | Neuf sans étiquette | Très bon état | Bon état | Satisfaisant (string ou null)",
  "description_optimisee": "description vendeuse en français pour Vinted, 3-4 phrases percutantes qui donnent envie d'acheter (string)",
  "query_vinted": "requête de recherche optimisée pour Vinted, inclure marque + type + taille si disponibles, ex: veste jean Levi\\'s M (string)"
}"""


def _build_prompt(image_count: int, description: Optional[str]) -> str:
    if image_count > 1 and description:
        intro = (
            f"Voici {image_count} photos d'un même vêtement sous différents angles, "
            f"accompagnées de cette description : « {description} »."
        )
    elif image_count > 1:
        intro = f"Voici {image_count} photos d'un même vêtement vues sous différents angles."
    elif image_count == 1 and description:
        intro = f"Voici une photo de vêtement accompagnée de cette description : « {description} »."
    elif image_count == 1:
        intro = "Voici une photo de vêtement."
    else:
        intro = f"Voici la description d'un vêtement : « {description} »."
    return f"{intro}\n\n{_SCHEMA}"


_LEGIT_SYSTEM = (
    "Tu es un expert en authentification de vêtements et maroquinerie de luxe. "
    "Tu as une connaissance approfondie des détails de fabrication : coutures, étiquettes, "
    "logos, polices, finitions, hardware, pour des marques comme Nike, Jordan, Supreme, "
    "Louis Vuitton, Gucci, Balenciaga, Off-White, Yeezy, Stone Island, Moncler, etc. "
    "Réponds UNIQUEMENT avec un objet JSON valide, sans texte supplémentaire, "
    "sans bloc markdown, sans explication."
)

_LEGIT_SCHEMA = """\
Analyse ces photos et retourne un JSON avec exactement ces clés :
{
  "marque_detectee": "marque identifiée (string ou null)",
  "verdict": "un parmi : Authentique | Suspect | Contrefaçon probable",
  "confiance": nombre entier entre 0 et 100,
  "details": [
    "observation précise 1 (ex: coutures irrégulières, police du logo incorrecte…)",
    "observation précise 2",
    "observation précise 3"
  ],
  "resume": "synthèse en 1-2 phrases du verdict avec les éléments clés observés"
}"""


def legit_check(images: list[tuple[bytes, str]]) -> dict:
    """
    Authenticate a clothing/luxury item via Claude vision.
    images: list of (data_bytes, mime_type) tuples (up to 5).
    Returns a dict with keys: marque_detectee, verdict, confiance, details, resume.
    """
    content = []
    for data, mime in images:
        b64 = base64.standard_b64encode(data).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
    content.append({"type": "text", "text": _LEGIT_SCHEMA})

    response = _client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_LEGIT_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    return json.loads(raw)


_PHOTO_IA_SYSTEM = (
    "You are a fashion expert and professional product photographer. "
    "Analyze clothing photos to create precise DALL-E 3 prompts for professional product shots. "
    "Respond ONLY with a valid JSON object, no extra text, no markdown blocks."
)

_PHOTO_IA_SCHEMA = """\
Analyze this clothing photo and return a JSON with exactly these keys:
{
  "description_fr": "description précise en français de l'article : type de vêtement, couleur principale, matière apparente, marque si visible, détails notables (boutons, broderies, motifs, coupe...)",
  "dalle_prompt": "Professional e-commerce product photo of a [describe the exact garment: type, color, fabric, visible brand, key details such as buttons/zippers/patterns/cut], displayed on a pure white background, studio lighting, sharp focus, high resolution, no model, ghost mannequin or flat lay presentation, Vinted-style product photo"
}"""


def describe_for_dalle(images: list[tuple[bytes, str]]) -> dict:
    """
    Analyze a clothing photo with Claude and return a description + DALL-E 3 prompt.
    images: list of (data_bytes, mime_type) tuples (1 image expected).
    Returns a dict with keys: description_fr, dalle_prompt.
    """
    content = []
    for data, mime in images:
        b64 = base64.standard_b64encode(data).decode("utf-8")
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
    content.append({"type": "text", "text": _PHOTO_IA_SCHEMA})

    response = _client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=_PHOTO_IA_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    return json.loads(raw)


def analyze_clothing(
    images: list[tuple[bytes, str]],
    description: Optional[str],
) -> dict:
    """
    Analyze a clothing item via Prizzy IA vision + text.
    images: list of (data_bytes, mime_type) tuples (up to 5).
    Returns a dict with keys: marque, type_vetement, couleur, taille,
    etat_estime, description_optimisee, query_vinted.
    """
    content = []

    for data, mime in images:
        b64 = base64.standard_b64encode(data).decode("utf-8")
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": b64,
            },
        })

    content.append({"type": "text", "text": _build_prompt(len(images), description)})

    response = _client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{"role": "user", "content": content}],
    )

    raw = next(b.text for b in response.content if b.type == "text").strip()
    # Strip markdown code fences if the model adds them despite the system prompt
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    return json.loads(raw)
