import unittest
from lib.util import format_satoshis, parse_URI

class TestUtil(unittest.TestCase):

    def test_format_satoshis(self):
        result = format_satoshis(1234)
        expected = "0.00001234"
        self.assertEqual(expected, result)

    def test_format_satoshis_diff_positive(self):
        result = format_satoshis(1234, is_diff=True)
        expected = "+0.00001234"
        self.assertEqual(expected, result)

    def test_format_satoshis_diff_negative(self):
        result = format_satoshis(-1234, is_diff=True)
        expected = "-0.00001234"
        self.assertEqual(expected, result)

    def _do_test_parse_URI(self, uri, expected):
        result = parse_URI(uri)
        self.assertEqual(expected, result)

    def test_parse_URI_address(self):
        self._do_test_parse_URI('viacoin:Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE',
                                {'address': 'Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE'})

    def test_parse_URI_only_address(self):
        self._do_test_parse_URI('Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE',
                                {'address': 'Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE'})


    def test_parse_URI_address_label(self):
        self._do_test_parse_URI('viacoin:Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE?label=electrum%20test',
                                {'address': 'Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE', 'label': 'electrum test'})

    def test_parse_URI_address_message(self):
        self._do_test_parse_URI('viacoin:Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE?message=electrum%20test',
                                {'address': 'Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE', 'message': 'electrum test', 'memo': 'electrum test'})

    def test_parse_URI_address_amount(self):
        self._do_test_parse_URI('viacoin:Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE?amount=0.0003',
                                {'address': 'Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE', 'amount': 30000})

    def test_parse_URI_address_request_url(self):
        self._do_test_parse_URI('viacoin:Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE?r=http://domain.tld/page?h%3D2a8628fc2fbe',
                                {'address': 'Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE', 'r': 'http://domain.tld/page?h=2a8628fc2fbe'})

    def test_parse_URI_ignore_args(self):
        self._do_test_parse_URI('viacoin:Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE?test=test',
                                {'address': 'Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE', 'test': 'test'})

    def test_parse_URI_multiple_args(self):
        self._do_test_parse_URI('viacoin:Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE?amount=0.00004&label=electrum-test&message=electrum%20test&test=none&r=http://domain.tld/page',
                                {'address': 'Vem9EJC7DfuuaM13TfH2NqY3hPNGVPNniE', 'amount': 4000, 'label': 'electrum-test', 'message': u'electrum test', 'memo': u'electrum test', 'r': 'http://domain.tld/page', 'test': 'none'})

    def test_parse_URI_no_address_request_url(self):
        self._do_test_parse_URI('viacoin:?r=http://domain.tld/page?h%3D2a8628fc2fbe',
                                {'r': 'http://domain.tld/page?h=2a8628fc2fbe'})

    def test_parse_URI_invalid_address(self):
        self.assertRaises(BaseException, parse_URI, 'viacoin:invalidaddress')

    def test_parse_URI_invalid(self):
        self.assertRaises(BaseException, parse_URI, 'notviacoin:LectrumELqJWMECz7W2iarBpT4VvAPqwAv')

    def test_parse_URI_parameter_polution(self):
        self.assertRaises(Exception, parse_URI, 'viacoin:LectrumELqJWMECz7W2iarBpT4VvAPqwAv?amount=0.0003&label=test&amount=30.0')

