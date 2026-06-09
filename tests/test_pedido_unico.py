import unittest
from unittest.mock import patch

import api_server


class RespostaFake:
    def __init__(self, status_code, data=None, text=""):
        self.status_code = status_code
        self._data = data
        self.text = text

    def json(self):
        return self._data


class PedidoUnicoTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_reutiliza_pedido_aberto_do_mesmo_numero(self, get, post):
        get.return_value = RespostaFake(200, [{
            "id": 8421,
            "phone": "5521999999999",
            "peca": "Farol esquerdo",
            "status": "aguardando",
        }])

        resposta = self.client.post("/integracoes/marcelo/pedido-unico", json={
            "phone": "21 99999-9999",
            "peca": "Farol esquerdo",
        })

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json["duplicado"])
        self.assertEqual(resposta.json["pedido"]["id"], 8421)
        post.assert_not_called()

    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_cria_quando_numero_nao_tem_pedido_aberto(self, get, post):
        get.return_value = RespostaFake(200, [])
        post.return_value = RespostaFake(201, [{
            "id": 8422,
            "phone": "5521999999999",
            "peca": "Farol esquerdo",
            "status": "aguardando",
        }])

        resposta = self.client.post("/integracoes/marcelo/pedido-unico", json={
            "phone": "5521999999999@c.us",
            "nome": "Cliente",
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
        })

        self.assertEqual(resposta.status_code, 201)
        self.assertTrue(resposta.json["criado"])
        self.assertFalse(resposta.json["duplicado"])
        self.assertEqual(post.call_args.kwargs["json"]["status"], "aguardando")

    def test_exige_telefone_e_peca(self):
        resposta = self.client.post("/integracoes/marcelo/pedido-unico", json={
            "phone": "21999999999",
        })

        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(resposta.json["ok"])


if __name__ == "__main__":
    unittest.main()
