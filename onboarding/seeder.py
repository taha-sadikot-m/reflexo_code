"""Vertical-specific knowledge seeding after onboarding completion."""

from __future__ import annotations

import logging
from typing import Any

from mcp_servers.reflexo_core.search.embeddings import embed
from mcp_servers.reflexo_core.utils.db_helpers import tenant_connection, vec_literal

logger = logging.getLogger(__name__)

ECOMMERCE_SEEDS: list[dict[str, str]] = [
    {
        "question": "How do I track my order?",
        "answer": (
            "Please share your order number or the phone number you used while ordering, "
            "and I'll check the status for you."
        ),
        "category": "shipping",
    },
    {
        "question": "Can I cancel my order?",
        "answer": (
            "I'll check if your order can still be cancelled. Please share your order number. "
            "Orders that have already been shipped cannot be cancelled."
        ),
        "category": "policy",
    },
    {
        "question": "I want to change my order",
        "answer": (
            "Please share your order number and what you'd like to change. "
            "I'll check if modifications are still possible. Once an order is shipped, "
            "changes cannot be made."
        ),
        "category": "policy",
    },
    {
        "question": "Where is my order?",
        "answer": (
            "I'd be happy to help track your order. Please share your order number or the "
            "phone number/email you used while placing the order."
        ),
        "category": "shipping",
    },
    {
        "question": "Do you have this in a different size?",
        "answer": "Let me check the available sizes for you. Which product are you looking at?",
        "category": "product",
    },
    {
        "question": "Do you have this in a different color?",
        "answer": (
            "Let me check available colors for that product. "
            "Can you tell me which product you're interested in?"
        ),
        "category": "product",
    },
    {
        "question": "Is this product available?",
        "answer": "Let me check the availability for you. Which product are you looking for?",
        "category": "product",
    },
    {
        "question": "What is the material/fabric?",
        "answer": "I'll check the material details for that product. Which item are you asking about?",
        "category": "product",
    },
    {
        "question": "How do I choose the right size?",
        "answer": (
            "You can refer to our size chart for measurements. If you share your height, "
            "weight, and usual size, I can suggest the best fit for you."
        ),
        "category": "product",
    },
    {
        "question": "Yeh mujhe fit hoga?",
        "answer": (
            "Aapki height, weight, aur usual size bataiye, main aapko sahi size suggest karunga. "
            "Size chart bhi bhej sakta hoon."
        ),
        "category": "product",
    },
    {
        "question": "Is online payment safe?",
        "answer": (
            "Yes, online payment is completely safe. We use secure payment gateways (Razorpay) "
            "that encrypt your card and UPI details. Your payment information is never stored with us."
        ),
        "category": "payment",
    },
    {
        "question": "Payment fail ho gaya / My payment failed",
        "answer": (
            "Sorry about that! If money was deducted, it will be automatically refunded within "
            "5-7 business days. Please try placing the order again, or use a different payment method."
        ),
        "category": "payment",
    },
    {
        "question": "EMI available hai?",
        "answer": (
            "EMI availability depends on your bank and card. During checkout, you can check if "
            "EMI options are available for your payment method."
        ),
        "category": "payment",
    },
    {
        "question": "Do you have any offers or discounts?",
        "answer": (
            "Let me check if there are any current offers or discount codes available. "
            "I'll get back to you with the details."
        ),
        "category": "general",
    },
]

UNIVERSAL_SEEDS: list[dict[str, str]] = [
    {
        "question": "How do I contact you?",
        "answer": (
            "You're already talking to me! I'm here to help 24/7. "
            "For anything I can't resolve, I'll connect you with the team."
        ),
        "category": "general",
    },
    {
        "question": "Kya koi offer chal raha hai?",
        "answer": (
            "Main check karta hoon ki koi current offer ya discount available hai. "
            "Aapko bata deta hoon."
        ),
        "category": "general",
    },
    {
        "question": "What are your timings?",
        "answer": "Let me check the schedule for you. One moment.",
        "category": "general",
    },
]

SALON_SEEDS: list[dict[str, str]] = [
    {
        "question": "Kya appointment zaroori hai?",
        "answer": (
            "Walk-in bhi le sakte hain, lekin appointment se waiting nahi hogi. "
            "Main abhi slot book kar sakta hoon."
        ),
        "category": "booking",
    },
    {
        "question": "How much is a haircut?",
        "answer": "Let me share the price list for you. Which service are you interested in?",
        "category": "pricing",
    },
]

RESTAURANT_SEEDS: list[dict[str, str]] = [
    {
        "question": "Delivery karte ho?",
        "answer": "Haan, delivery available hai. Aap apna order bata dijiye, main process kar deta hoon.",
        "category": "delivery",
    },
    {
        "question": "Table book karna hai",
        "answer": "Kitne logo ke liye aur kab aana chahte hain? Main reservation kar deta hoon.",
        "category": "booking",
    },
]

FREELANCER_SEEDS: list[dict[str, str]] = [
    {
        "question": "Portfolio dikha do",
        "answer": "Main aapko portfolio share karta hoon. Kis type ka kaam dekhna hai?",
        "category": "portfolio",
    },
    {
        "question": "Kitna time lagega?",
        "answer": (
            "Timeline project ke scope pe depend karta hai. "
            "Requirements share karo toh estimate de sakta hoon."
        ),
        "category": "process",
    },
]

INFLUENCER_SEEDS: list[dict[str, str]] = [
    {
        "question": "Collaboration rates kya hain?",
        "answer": (
            "Rates platform aur content type pe depend karte hain. "
            "Kaunsa platform aur content type chahiye?"
        ),
        "category": "pricing",
    },
]

TUTOR_SEEDS: list[dict[str, str]] = [
    {
        "question": "Demo class milegi?",
        "answer": "Haan, ek free demo class le sakte hain. Kab convenient hoga?",
        "category": "booking",
    },
]

CONSULTANT_SEEDS: list[dict[str, str]] = [
    {
        "question": "Initial consultation free hai?",
        "answer": (
            "Pehli consultation ke details main share karta hoon. "
            "Kya topic pe guidance chahiye?"
        ),
        "category": "booking",
    },
]

FITNESS_SEEDS: list[dict[str, str]] = [
    {
        "question": "Trial class milegi?",
        "answer": "Haan, trial class available hai. Kaunsi class try karna chahte hain?",
        "category": "booking",
    },
]

VERTICAL_MAP: dict[str, list[dict[str, str]]] = {
    "ecommerce": ECOMMERCE_SEEDS,
    "salon": SALON_SEEDS,
    "restaurant": RESTAURANT_SEEDS,
    "freelancer": FREELANCER_SEEDS,
    "influencer": INFLUENCER_SEEDS,
    "tutor": TUTOR_SEEDS,
    "consultant": CONSULTANT_SEEDS,
    "fitness": FITNESS_SEEDS,
}


def seeds_for_vertical(vertical: str) -> list[dict[str, str]]:
    v = (vertical or "general").lower().strip()
    return list(UNIVERSAL_SEEDS) + list(VERTICAL_MAP.get(v, []))


async def seed_vertical_knowledge(business_id: str, vertical: str) -> int:
    """
    Seed KB with vertical-specific Q&As (source=system_seed, confidence=0.7).
    Skips near-duplicates. Returns number of entries inserted.
    """
    seeds = seeds_for_vertical(vertical)
    seeded = 0

    for seed in seeds:
        question = seed.get("question") or ""
        answer = seed["answer"]
        category = seed.get("category") or "general"
        text = f"{question} {answer}".strip()
        try:
            embedding = await embed(text)
        except Exception as exc:
            logger.warning("Skip seed embed failed for %r: %s", question[:40], exc)
            continue

        vec = vec_literal(embedding)
        try:
            async with tenant_connection(business_id) as conn:
                dup = await conn.fetchval(
                    """
                    SELECT id FROM knowledge_entries
                    WHERE business_id = $1::uuid AND is_active = true
                      AND embedding <=> $2::vector < 0.15
                    LIMIT 1
                    """,
                    business_id,
                    vec,
                )
                if dup:
                    continue
                await conn.execute(
                    """
                    INSERT INTO knowledge_entries (
                        business_id, question, answer, category, embedding, source, confidence
                    )
                    VALUES ($1::uuid, $2, $3, $4, $5::vector, 'system_seed', 0.7)
                    """,
                    business_id,
                    question or None,
                    answer,
                    category,
                    vec,
                )
                seeded += 1
        except Exception as exc:
            logger.warning("Skip seed insert failed for %r: %s", question[:40], exc)

    logger.info(
        "Seeded %d knowledge entries for business=%s vertical=%s",
        seeded,
        business_id,
        vertical,
    )
    return seeded
