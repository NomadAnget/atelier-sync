"""Previously registered CPU composition checks, applied to the exact candidate pipeline."""
import unittest
from src.pipelines.video.units.alignment import plan_alignment
from src.pipelines.video.units.audio_unit import AudioUnit, TranslationPair, UnitType

class TestS06Alignment(unittest.TestCase):

    def test_cross_speaker_spill_no_collision(self):
        """构造用例: A 说话人配音超长溢出,紧随其后 B 说话人接话,B 绝不能与 A 撞车。"""
        from src.pipelines.video.units.audio_unit import Speaker
        spk_a = Speaker(id='SPK_A')
        spk_b = Speaker(id='SPK_B')
        p_a = TranslationPair(start=0.0, end=2.0, src_text='Hello from A', translation='我是A', tts_duration=3.0, tts_path='dummy_a.wav')
        u_a = AudioUnit(start=0.0, end=2.0, kind=UnitType.SPEECH, speaker=spk_a, pairs=[p_a])
        p_b = TranslationPair(start=2.1, end=4.1, src_text='Hello from B', translation='我是B', tts_duration=2.0, tts_path='dummy_b.wav')
        u_b = AudioUnit(start=2.1, end=4.1, kind=UnitType.SPEECH, speaker=spk_b, pairs=[p_b])
        units = {'SPK_A': [u_a], 'SPK_B': [u_b]}
        _, aplan, smap = plan_alignment(units, source_duration=5.0)
        rows = {r['speaker']: r for r in smap}
        self.assertGreaterEqual(rows['SPK_B']['out_start'], rows['SPK_A']['out_end'] - 0.001, f"SPK_B 撞车: B_start={rows['SPK_B']['out_start']} < A_end={rows['SPK_A']['out_end']}")

    def test_non_binding_member_not_slowed(self):
        """构造用例: 真源重叠组内, 非绑定成员配音较长时不吃主说话人的放慢。"""
        from src.pipelines.video.units.audio_unit import Speaker
        spk_a = Speaker(id='SPK_A')
        spk_b = Speaker(id='SPK_B')
        p_a = TranslationPair(start=0.0, end=10.0, src_text='Main long speech', translation='主说话人长句', tts_duration=5.0, tts_path='dummy_a.wav')
        u_a = AudioUnit(start=0.0, end=10.0, kind=UnitType.SPEECH, speaker=spk_a, pairs=[p_a])
        p_b = TranslationPair(start=1.0, end=2.0, src_text='Quick interjection', translation='短插话', tts_duration=1.8, tts_path='dummy_b.wav')
        u_b = AudioUnit(start=1.0, end=2.0, kind=UnitType.SPEECH, speaker=spk_b, pairs=[p_b])
        units = {'SPK_A': [u_a], 'SPK_B': [u_b]}
        _, _, smap = plan_alignment(units, source_duration=10.0)
        rows = {r['speaker']: r for r in smap}
        self.assertGreater(rows['SPK_B']['atempo'], 1.0, f"非绑定成员 B 不应被放慢: atempo={rows['SPK_B']['atempo']}")

