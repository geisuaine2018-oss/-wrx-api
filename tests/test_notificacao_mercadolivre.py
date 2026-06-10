import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import api_server


class RespostaFake:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data or {}
        self.text = ""

    def json(self):
        return self._data


class NotificacaoMercadoLivreTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.integ_patch = patch.object(api_server, "_INTEG_DIR", self.temp.name)
        self.avisados_patch = patch.object(
            api_server,
            "_AVISADOS_FILE",
            os.path.join(self.temp.name, "wrx_whatsapp_avisados.json"),
        )
        self.integ_patch.start()
        self.avisados_patch.start()
        with open(api_server._AVISADOS_FILE, "w", encoding="utf-8") as arquivo:
            json.dump(["seed"], arquivo)

    def tearDown(self):
        self.avisados_patch.stop()
        self.integ_patch.stop()
        self.temp.cleanup()

    @patch("api_server._waha_numero_sessao", return_value="5521999999999")
    @patch("api_server._ml_get_user_token", return_value="token")
    @patch(
        "api_server._ml_load_tokens",
        return_value={"default": {"user_id": "123"}},
    )
    @patch("api_server.requests.get")
    @patch("api_server._waha_enviar")
    def test_venda_so_e_marcada_depois_de_envio_confirmado(
        self, enviar, get, _tokens, _token, _numero
    ):
        venda = {
            "id": 987654,
            "date_created": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "total_amount": 199.9,
            "order_items": [{"item": {"title": "Farol direito"}}],
        }

        def resposta_api(url, **_kwargs):
            if "orders/search" in url:
                return RespostaFake(200, {"results": [venda]})
            if "questions/search" in url:
                return RespostaFake(200, {"questions": []})
            return RespostaFake(200, {"data": []})

        get.side_effect = resposta_api
        enviar.side_effect = [(False, "WAHA indisponivel"), (True, "ok")]

        primeira = self.client.post("/integracoes/whatsapp/checar-novidades")
        with open(api_server._AVISADOS_FILE, encoding="utf-8") as arquivo:
            avisados_primeira = set(json.load(arquivo))

        segunda = self.client.post("/integracoes/whatsapp/checar-novidades")
        with open(api_server._AVISADOS_FILE, encoding="utf-8") as arquivo:
            avisados_segunda = set(json.load(arquivo))

        self.assertEqual(primeira.json["enviados"], 0)
        self.assertNotIn("venda:987654", avisados_primeira)
        self.assertEqual(segunda.json["enviados"], 1)
        self.assertIn("venda:987654", avisados_segunda)


if __name__ == "__main__":
    unittest.main()
