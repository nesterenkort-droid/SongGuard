from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import settings
from app.redis_client import redis_client
from app.services import ai_judge

pytestmark = pytest.mark.asyncio


async def test_ai_judge_budget_cap():
    # 1. Clear spend in Redis
    key = ai_judge._get_spend_key()
    await redis_client.delete(key)
    
    # 2. Check spend is initially 0
    assert await ai_judge.get_monthly_spend() == 0.0
    
    # 3. Increment spend
    await ai_judge._increment_spend(1.50)
    assert await ai_judge.get_monthly_spend() == 1.50
    
    # 4. Increment spend past the budget cap
    # Default cap is settings.ai_judge_monthly_budget_usd (10.0)
    await ai_judge._increment_spend(9.00)
    assert await ai_judge.get_monthly_spend() == 10.50
    
    # Temporarily set API key so it doesn't fail on missing API key check
    with patch.object(settings, "anthropic_api_key", "mock-key"):
        result = await ai_judge.evaluate_candidate(
            track_title="Test Track",
            track_artist="Test Artist",
            candidate_title="Suspicious",
            candidate_uploader="PirateUploader",
            candidate_description="None",
            candidate_platform="youtube",
            duration_diff_sec=2.0,
            score_before_ai=50,
            audio_matched=False
        )
        assert result.verdict == "skipped"
        assert "лимит" in result.reasoning_ru.lower()


async def test_ai_judge_mock_response():
    # 1. Clear spend in Redis
    key = ai_judge._get_spend_key()
    await redis_client.delete(key)
    
    # Mock Response from Claude
    mock_response = {
        "content": [
            {
                "text": (
                    '{"verdict": "pirate", "confidence": 95, '
                    '"reasoning_ru": "Название совпадает, описание содержит '
                    'Provided to YouTube by DistroKid."}'
                )
            }
        ],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 150
        }
    }
    
    # Setup patches
    with patch.object(settings, "anthropic_api_key", "mock-key"):
        # Mock httpx POST request
        mock_post = AsyncMock()
        mock_post.return_value = httpx.Response(200, json=mock_response)
        
        with patch("httpx.AsyncClient.post", mock_post):
            result = await ai_judge.evaluate_candidate(
                track_title="Test Track",
                track_artist="Test Artist",
                candidate_title="Suspicious",
                candidate_uploader="PirateUploader",
                candidate_description="Provided to YouTube by DistroKid",
                candidate_platform="youtube",
                duration_diff_sec=0.0,
                score_before_ai=55,
                audio_matched=True,
                audio_true_stretch=1.0
            )
            
            assert result.verdict == "pirate"
            assert result.confidence == 95
            assert "DistroKid" in result.reasoning_ru
            
            # Check cost calculations
            # Input: 1000 * 0.80/M = 0.0008, Output: 150 * 4.00/M = 0.0006
            # Total cost: 0.0014
            assert abs(result.cost_usd - 0.0014) < 1e-6
            assert abs(await ai_judge.get_monthly_spend() - 0.0014) < 1e-6
