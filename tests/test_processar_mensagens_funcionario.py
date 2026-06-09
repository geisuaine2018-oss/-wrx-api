import json
import os
import tempfile
import unittest
from unittest.mock import patch

import api_server


class RespostaFake:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class ProcessarMensagensFuncionarioTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.integ_patch = patch.object(api_server, "_INTEG_DIR", self.temp.name)
        self.integ_patch.start()

    def tearDown(self):
        self.integ_patch.stop()
        self.temp.cleanup()

    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_primeira_execucao_so_marca_historico(self, get, post):
        get.return_value = RespostaFake(200, [{
            "id": 10,
            "numero": api_server.FUNCS_PEDIDO["robson"],
            "mensagem": "Tenho #8421-1",
            "de_mim": False,
            "criado_em": "2026-06-09T10:00:00Z",
        }])

        resposta = self.client.post(
            "/integracoes/whatsapp/processar-respostas-funcionarios"
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["seed"], 1)
        post.assert_not_called()

    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_processa_somente_nova_resposta_valida(self, get, post):
        estado = os.path.join(self.temp.name, "mensagens_func_processadas.json")
        with open(estado, "w", encoding="utf-8") as arquivo:
            json.dump(["10"], arquivo)
        get.return_value = RespostaFake(200, [
            {
                "id": 11,
                "numero": api_server.FUNCS_PEDIDO["rafael"],
                "mensagem": "Nao tenho #8421-2",
                "de_mim": False,
                "criado_em": "2026-06-09T10:02:00Z",
            },
            {
                "id": 10,
                "numero": api_server.FUNCS_PEDIDO["rafael"],
                "mensagem": "Tenho #8421-1",
                "de_mim": False,
                "criado_em": "2026-06-09T10:00:00Z",
            },
        ])
        post.return_value = RespostaFake(200, {"ok": True})

        resposta = self.client.post(
            "/integracoes/whatsapp/processar-respostas-funcionarios"
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["novas"], 1)
        self.assertEqual(resposta.json["processadas"], 1)
        self.assertEqual(post.call_args.kwargs["json"]["item_id"], "8421-2")


if __name__ == "__main__":
    unittest.main()
