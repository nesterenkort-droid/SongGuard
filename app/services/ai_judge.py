import json
import logging
from datetime import datetime
from typing import NamedTuple

import httpx

from app.config import settings
from app.redis_client import redis_client

logger = logging.getLogger(__name__)

# Claude 3.5 Haiku pricing: Input $0.80/MTok, Output $4.00/MTok
CLAUDE_INPUT_COST_PER_TOKEN = 0.80 / 1_000_000
CLAUDE_OUTPUT_COST_PER_TOKEN = 4.00 / 1_000_000


class JudgeVerdict(NamedTuple):
    verdict: str  # "pirate" | "remix" | "safe" | "skipped"
    confidence: int  # 0-100
    reasoning_ru: str
    cost_usd: float = 0.0


def _get_spend_key() -> str:
    now = datetime.now()
    return f"ai_judge:spend:{now.year}-{now.month:02d}"


async def get_monthly_spend() -> float:
    """Returns the total USD spent on AI Judge in the current calendar month."""
    key = _get_spend_key()
    val = await redis_client.get(key)
    if val is None:
        return 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0


async def _increment_spend(amount_usd: float) -> float:
    """Increments the monthly USD spend in Redis."""
    key = _get_spend_key()
    new_val = await redis_client.incrbyfloat(key, amount_usd)
    await redis_client.expire(key, 3600 * 24 * 31)
    return new_val


async def evaluate_candidate(
    track_title: str,
    track_artist: str,
    candidate_title: str,
    candidate_uploader: str,
    candidate_description: str,
    candidate_platform: str,
    duration_diff_sec: float | None,
    score_before_ai: int,
    audio_matched: bool,
    audio_true_stretch: float | None = None,
) -> JudgeVerdict:
    """Evaluate a suspicious candidate via Claude to decide piracy vs. remix vs. safe.

    Applies a strict monthly budget check; returns a skipped verdict if the cap is
    reached or no API key is configured.
    """
    # 1. Check API Key
    if not settings.anthropic_api_key:
        logger.warning("Anthropic API key is not configured, skipping AI Judge evaluation")
        return JudgeVerdict(
            verdict="skipped",
            confidence=0,
            reasoning_ru="Claude API ключ не задан. Находка отправлена на ручную проверку.",
        )

    # 2. Check Budget Cap
    current_spend = await get_monthly_spend()
    if current_spend >= settings.ai_judge_monthly_budget_usd:
        logger.warning(
            "AI Judge monthly budget cap reached (%.4f$ >= %s$). Skipping.",
            current_spend, settings.ai_judge_monthly_budget_usd,
        )
        return JudgeVerdict(
            verdict="skipped",
            confidence=0,
            reasoning_ru=(
                "Превышен месячный лимит расходов на ИИ-судью. "
                "Находка отправлена на ручную проверку."
            ),
        )

    # 3. Formulate Prompt
    system_prompt = (
        "Ты — ИИ-судья в системе обнаружения музыкального пиратства TrackGuard.\n"
        "Твоя задача: проанализировать метаданные оригинального трека и найденного "
        "подозрительного кандидата и определить, является ли кандидат:\n"
        "1. \"pirate\" — пиратской копией (замедленный/ускоренный slowed/sped-up "
        "рендер, прямой перезалив, гомоглифы в названии).\n"
        "2. \"remix\" — неофициальным ремиксом, кавером или авторской работой, "
        "вдохновленной оригиналом (требует ручной оценки).\n"
        "3. \"safe\" — полностью легальной или не связанной с оригиналом копией.\n\n"
        "Выведи результат строго в формате JSON со следующими полями:\n"
        "{\n"
        "  \"verdict\": \"pirate\" | \"remix\" | \"safe\",\n"
        "  \"confidence\": число от 0 до 100,\n"
        "  \"reasoning_ru\": \"Краткое объяснение решения на русском языке\"\n"
        "}"
    )

    user_content = (
        f"Оригинальный трек:\n"
        f"- Название: {track_title}\n"
        f"- Артист: {track_artist}\n\n"
        f"Подозрительный кандидат:\n"
        f"- Название: {candidate_title}\n"
        f"- Платформа: {candidate_platform}\n"
        f"- Загрузил: {candidate_uploader}\n"
        f"- Разница длительности: "
        f"{f'{duration_diff_sec} сек.' if duration_diff_sec is not None else 'Неизвестно'}\n"
        f"- Предварительный скор метаданных: {score_before_ai} / 100\n"
        f"- Совпадение по спектрограмме Panako: {'Да' if audio_matched else 'Нет'}\n"
        f"- Коэффициент растяжения звука: "
        f"{f'{audio_true_stretch:.3f}x' if audio_true_stretch else 'N/A'}\n"
        f"- Описание релиза: {candidate_description[:600]}\n"
    )

    # 4. Make Request
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 500,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages", headers=headers, json=body
            )
            if response.status_code != 200:
                logger.error(
                    "Claude API request failed with status %s: %s",
                    response.status_code, response.text,
                )
                return JudgeVerdict(
                    verdict="skipped",
                    confidence=0,
                    reasoning_ru=(
                        "Запрос к Claude API завершился ошибкой. "
                        "Находка отправлена на ручную проверку."
                    ),
                )

            res_json = response.json()
            content_text = res_json["content"][0]["text"].strip()
            usage = res_json.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

            # Calculate cost
            cost = (
                input_tokens * CLAUDE_INPUT_COST_PER_TOKEN
                + output_tokens * CLAUDE_OUTPUT_COST_PER_TOKEN
            )
            await _increment_spend(cost)

            # Parse JSON response from Claude
            data = json.loads(content_text)
            return JudgeVerdict(
                verdict=data.get("verdict", "skipped"),
                confidence=data.get("confidence", 0),
                reasoning_ru=data.get("reasoning_ru", "Обоснование отсутствует"),
                cost_usd=cost,
            )

    except Exception as e:
        logger.error("Error executing Claude AI Judge: %s", e)
        return JudgeVerdict(
            verdict="skipped",
            confidence=0,
            reasoning_ru=(
                f"Исключение при работе ИИ-судьи: {e}. Находка отправлена на ручную проверку."
            ),
        )
