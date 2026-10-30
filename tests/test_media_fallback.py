import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from providers.base import GenerationResult, ProviderContentPolicyError, ProviderUnavailableError
from providers.router import get_provider_chain_for_model
from providers.base import TaskType
from config import config
from services import media_service as media
from bot.keyboards.media import model_top_kb


class MediaFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_moderation_fallback_and_billing(self):
        operations = {
            'generate_image': dict(prompt='test', aspect_ratio='1:1', resolution='1K'),
            'generate_video': dict(prompt='test', duration=5),
            'generate_audio': dict(prompt='test', audio_type='music'),
            'edit_image': dict(image_bytes=b'image', prompt='test'),
            'edit_video': dict(video_bytes=b'video', prompt='test'),
        }
        for name, kwargs in operations.items():
            for outcome in ('success', 'policy', 'unavailable', 'no_reserve'):
                with self.subTest(operation=name, outcome=outcome):
                    primary = AsyncMock(side_effect=ProviderContentPolicyError('primary'))
                    result = GenerationResult(b'result', 'application/octet-stream', 'result')
                    reserve = AsyncMock(return_value=result)
                    reserve.__qualname__ = name
                    expected_error = None
                    if outcome == 'policy':
                        expected_error = ProviderContentPolicyError
                        reserve.side_effect = expected_error('reserve')
                    elif outcome == 'unavailable':
                        expected_error = ProviderUnavailableError
                        reserve.side_effect = expected_error('reserve')
                    elif outcome == 'no_reserve':
                        expected_error = ProviderContentPolicyError
                    cfg = dict(model_id='primary', provider='primary', cost_credits=5)
                    chain = [(SimpleNamespace(**{name: primary}), cfg)]
                    if outcome != 'no_reserve':
                        chain.append((SimpleNamespace(**{name: reserve}), {**cfg, 'model_id': 'reserve'}))
                    with (
                        patch.object(media, 'get_provider_chain_for_model', return_value=chain),
                        patch.object(media, '_check_preconditions', new=AsyncMock(return_value=123)),
                        patch.object(media, 'deduct_credits', new=AsyncMock()) as deduct,
                        patch.object(media, '_ensure_requested_duration', new=AsyncMock(return_value=b'result')),
                        patch.object(media.asyncio, 'sleep', new=AsyncMock()),
                    ):
                        call = getattr(media, name)(telegram_id=1, username='test', model_slug='test', **kwargs)
                        if expected_error:
                            with self.assertRaises(expected_error):
                                await call
                            deduct.assert_not_awaited()
                        else:
                            actual = await call
                            self.assertEqual(actual.data, b'result')
                            deduct.assert_awaited_once_with(123, 5, {
                                'generate_image': 'image_generation', 'generate_video': 'video_generation',
                                'generate_audio': 'audio_generation', 'edit_image': 'image_edit',
                                'edit_video': 'video_edit',
                            }[name])
                        primary.assert_awaited_once()
                        self.assertEqual(reserve.await_count, 0 if outcome == 'no_reserve' else 2 if outcome == 'unavailable' else 1)

    def test_seedream_catalog_and_stale_lite_requests(self):
        for task in (TaskType.IMAGE_GENERATION, TaskType.IMAGE_EDIT):
            models = config.models[task.value]['models']
            seedream = [m for m in models if m['id'].startswith('seedream-5')]
            enabled = [m for m in seedream if m.get('enabled')]
            self.assertEqual(len(enabled), 1)
            self.assertEqual(enabled[0]['label'], 'Seedream 5 Pro')
            kb = model_top_kb(enabled)
            self.assertEqual(kb.inline_keyboard[0][0].text, 'Seedream 5 Pro')
            self.assertTrue(kb.inline_keyboard[0][0].callback_data.startswith('media:model:'))
            lite = next(m for m in seedream if 'lite' in m['id'])
            with self.assertRaises(ProviderUnavailableError):
                get_provider_chain_for_model(task, lite['id'])


if __name__ == '__main__':
    unittest.main()
