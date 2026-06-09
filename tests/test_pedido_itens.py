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


class PedidoItensTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.integ_patch = patch.object(api_server, "_INTEG_DIR", self.temp.name)
        self.integ_patch.start()
        self.pedido = {
            "id": 8421,
            "phone": "5521999999999",
            "nome": "Cliente",
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "esquerdo",
            "status": "aguardando",
            "criado_em": "2026-06-09T10:00:00Z",
        }

    def tearDown(self):
        self.integ_patch.stop()
        self.temp.cleanup()

    @patch("api_server.requests.get")
    def test_adiciona_segunda_peca_ao_mesmo_pedido(self, get):
        get.return_value = RespostaFake(200, [self.pedido])

        resposta = self.client.post("/integracoes/marcelo/pedido-item", json={
            "pedido_id": 8421,
            "peca": "Lanterna traseira",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "direito",
            "origem": "manual",
        })

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(resposta.json["pedido"]["id"], 8421)
        self.assertEqual(resposta.json["item"]["id"], "8421-2")
        self.assertEqual(len(resposta.json["itens"]), 2)

    @patch("api_server.requests.get")
    def test_nao_repete_mesmo_item(self, get):
        get.return_value = RespostaFake(200, [self.pedido])
        payload = {
            "pedido_id": 8421,
            "peca": "Lanterna traseira",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "direito",
        }

        primeira = self.client.post("/integracoes/marcelo/pedido-item", json=payload)
        segunda = self.client.post("/integracoes/marcelo/pedido-item", json=payload)

        self.assertEqual(primeira.status_code, 201)
        self.assertEqual(segunda.status_code, 200)
        self.assertTrue(segunda.json["duplicado"])
        self.assertEqual(len(segunda.json["itens"]), 2)

    @patch("api_server.requests.get")
    def test_lista_item_original_e_itens_adicionados(self, get):
        get.return_value = RespostaFake(200, [self.pedido])
        self.client.post("/integracoes/marcelo/pedido-item", json={
            "pedido_id": 8421,
            "peca": "Retrovisor",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "direito",
        })

        resposta = self.client.get("/integracoes/marcelo/pedido-itens/8421")

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["total"], 2)
        self.assertEqual(resposta.json["itens"][0]["id"], "8421-1")
        self.assertEqual(resposta.json["itens"][1]["id"], "8421-2")


if __name__ == "__main__":
    unittest.main()
