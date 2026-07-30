"""Tests for KestrelbyASNGenerator — pure Python, no Odoo ORM required."""
import unittest


class TestKestrelbyASNGenerator(unittest.TestCase):

    def _make_despatch(self, **overrides):
        base = {
            'po_number': '4500038166',
            'despatch_ref': 'DASN-4500038166',
            'despatch_date': '20260305',
            # Both routing identities are supplied explicitly: neither has a
            # code default any more (they are account-specific configuration —
            # ir.config_parameter mml_edi.sender_id / mml_edi.kestrelby_buyer_gln,
            # read by services/edi_service.py). Fictional values on purpose.
            'van_sender_id': 'DEMOEDI',
            'ctrl_ref': '1',
            'buyer_gln': '0200099000008',
            'deliveries': [
                {
                    'store_gln': '1005',
                    'lines': [
                        {'ean13': '0200000375621', 'qty': 10, 'seq': 10},
                        {'ean13': '0200000375638', 'qty': 7, 'seq': 20},
                    ],
                },
                {
                    'store_gln': '1007',
                    'lines': [
                        {'ean13': '0200000375621', 'qty': 7, 'seq': 30},
                    ],
                },
            ],
        }
        base.update(overrides)
        return base

    def _get_generator(self):
        from mml_edi.parsers.kestrelby_asn import KestrelbyASNGenerator
        return KestrelbyASNGenerator()

    def test_generate_contains_required_segments(self):
        gen = self._get_generator()
        result = gen.generate(self._make_despatch())
        self.assertIn('DESADV:D:96A:UN:EAN008', result)
        self.assertIn('BGM+351', result)
        self.assertIn('RFF+ON:4500038166', result)
        self.assertIn('0200000375621:EN', result)
        self.assertIn('QTY+12:10:EA', result)
        self.assertIn('UNS+S', result)

    def test_line_count_in_cnt_equals_lin_count(self):
        gen = self._get_generator()
        result = gen.generate(self._make_despatch())
        segments = [s for s in result.split("'") if s.strip()]
        lin_count = sum(1 for s in segments if s.startswith('LIN+'))
        cnt_line = next(s for s in segments if s.startswith('CNT+2:'))
        count_in_cnt = int(cnt_line.split(':')[1])
        self.assertEqual(count_in_cnt, lin_count)

    def test_unt_segment_count_is_correct(self):
        gen = self._get_generator()
        result = gen.generate(self._make_despatch())
        segments = [s for s in result.split("'") if s.strip()]
        unt_line = next(s for s in segments if s.startswith('UNT+'))
        count_in_unt = int(unt_line.split('+')[1])
        # UNT count = segments between UNH and UNT inclusive (excludes UNB and UNZ)
        unb_idx = next(i for i, s in enumerate(segments) if s.startswith('UNB+'))
        unz_idx = next(i for i, s in enumerate(segments) if s.startswith('UNZ+'))
        expected = unz_idx - unb_idx - 1  # excludes UNB and UNZ
        self.assertEqual(count_in_unt, expected)

    def test_invalid_ean13_raises_value_error(self):
        gen = self._get_generator()
        bad = self._make_despatch()
        bad['deliveries'][0]['lines'][0]['ean13'] = '0200000375620'  # wrong check digit
        with self.assertRaises(ValueError):
            gen.generate(bad)

    def test_two_stores_produce_two_cps_segments(self):
        gen = self._get_generator()
        result = gen.generate(self._make_despatch())
        segments = [s for s in result.split("'") if s.strip()]
        cps_count = sum(1 for s in segments if s.startswith('CPS+'))
        self.assertEqual(cps_count, 2)

    def test_kestrelby_gln_appears_in_nad_by(self):
        gen = self._get_generator()
        result = gen.generate(self._make_despatch())
        self.assertIn('NAD+BY+0200099000008::14', result)

    def test_configured_identities_reach_the_wire_segments(self):
        """The extracted values must be what the outbound DESADV actually
        carries: van_sender_id in UNB S002 and NAD+SE, buyer_gln in UNB S003
        and NAD+BY. This is the "outbound document reflects the configured
        value" half of the config extraction."""
        gen = self._get_generator()
        result = gen.generate(self._make_despatch(
            van_sender_id='CFGSENDER', buyer_gln='0200099000008'))
        segments = [s for s in result.split("'") if s.strip()]
        unb = next(s for s in segments if s.startswith('UNB+'))
        self.assertIn('CFGSENDER:14', unb)
        self.assertIn('0200099000008:14', unb)
        self.assertIn('NAD+SE+CFGSENDER::14', result)
        self.assertIn('NAD+BY+0200099000008::14', result)

    def test_no_account_specific_default_is_baked_into_the_parser(self):
        """Fresh-install neutrality: the module carries no counterparty GLN."""
        from mml_edi.parsers.kestrelby_asn import DEFAULT_BUYER_GLN
        self.assertEqual(DEFAULT_BUYER_GLN, '')

    def test_missing_van_sender_id_fails_closed(self):
        gen = self._get_generator()
        despatch = self._make_despatch(van_sender_id='')
        with self.assertRaises(ValueError):
            gen.generate(despatch)

    def test_missing_buyer_gln_fails_closed(self):
        gen = self._get_generator()
        despatch = self._make_despatch(buyer_gln='')
        with self.assertRaises(ValueError):
            gen.generate(despatch)

    def test_special_characters_in_po_number_are_escaped(self):
        """EDIFACT delimiters in PO number must be escaped with the release character '?'."""
        gen = self._get_generator()
        despatch = self._make_despatch()
        despatch['po_number'] = "4500+038:166"  # contains + and : delimiters
        result = gen.generate(despatch)
        # The raw delimiters must NOT appear unescaped in the RFF segment
        rff_line = next(s for s in result.split("'") if s.startswith('RFF+ON:'))
        # The PO number after 'RFF+ON:' should have escaped delimiters
        po_in_output = rff_line[len('RFF+ON:'):]
        self.assertNotIn('+', po_in_output.replace('?+', ''),
                         "Unescaped '+' found in PO number segment")
        self.assertNotIn(':', po_in_output.replace('?:', ''),
                         "Unescaped ':' found in PO number segment")
        self.assertIn('?+', po_in_output, "'+' should be escaped as '?+'")
        self.assertIn('?:', po_in_output, "':' should be escaped as '?:'")
