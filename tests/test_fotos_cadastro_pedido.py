import json
import os
import tempfile
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


class FotosCadastroPedidoTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.integ_patch = patch.object(api_server, "_INTEG_DIR", self.temp.name)
        self.integ_patch.start()
        self.phone = api_server.FUNCS_PEDIDO["robson"]
        self.pedido = {
            "id": 8421,
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "esquerdo",
            "status": "atendimento",
            "criado_em": "2026-06-09T10:00:00Z",
        }
        with open(
            os.path.join(self.temp.name, "respostas_func.json"),
            "w",
            encoding="utf-8",
        ) as arquivo:
            json.dump({
                f"8421-1:{self.phone}:tenho": {
                    "item_id": "8421-1",
                    "acao": "tenho",
                    "funcionario": "Robson",
                }
            }, arquivo)

    def tearDown(self):
        self.integ_patch.stop()
        self.temp.cleanup()

    @patch("api_server.requests.get")
    def test_vincula_foto_depois_do_tenho(self, get):
        get.return_value = RespostaFake(200, [self.pedido])

        resposta = self.client.post(
            "/integracoes/marcelo/pedido-item-foto",
            json={
                "phone": self.phone,
                "item_id": "8421-1",
                "foto": "https://exemplo.com/farol.jpg",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["item"]["status"], "foto_recebida")
        self.assertEqual(len(resposta.json["item"]["fotos"]), 1)
        self.assertFalse(resposta.json["envio_cliente"])

    @patch("api_server._max_sku_numerico", return_value=109999)
    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_cadastra_produto_com_foto_sem_publicar(self, get, post, _max):
        get.return_value = RespostaFake(200, [self.pedido])
        post.return_value = RespostaFake(201, [{"sku": "110000"}])
        self.client.post(
            "/integracoes/marcelo/pedido-item-foto",
            json={
                "phone": self.phone,
                "item_id": "8421-1",
                "foto": "https://exemplo.com/farol.jpg",
            },
        )

        resposta = self.client.post(
            "/integracoes/marcelo/cadastrar-produto-encontrado",
            json={
                "phone": self.phone,
                "item_id": "8421-1",
                "preco": "650,00",
                "loc": "A1",
            },
        )

        self.assertEqual(resposta.status_code, 201)
        self.assertEqual(resposta.json["sku"], "110000")
        self.assertFalse(resposta.json["publicado"])
        produto = post.call_args.kwargs["json"]
        self.assertEqual(produto["qtd"], 1)
        self.assertEqual(produto["origem"], "pedido #8421")
        self.assertEqual(produto["cadastrado_por"], "Robson")

    @patch("api_server.requests.get")
    def test_nao_cadastra_sem_foto(self, get):
        get.return_value = RespostaFake(200, [self.pedido])

        resposta = self.client.post(
            "/integracoes/marcelo/cadastrar-produto-encontrado",
            json={"phone": self.phone, "item_id": "8421-1"},
        )

        self.assertEqual(resposta.status_code, 409)
        self.assertIn("foto", resposta.json["erro"])

    @patch("api_server._max_sku_numerico", return_value=109999)
    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_foto_cadastra_automaticamente_quando_nao_existe_sku(
        self, get, post, _max
    ):
        get.return_value = RespostaFake(200, [self.pedido])
        post.return_value = RespostaFake(201, [{"sku": "110000"}])
        with open(
            os.path.join(self.temp.name, "pedido_itens.json"),
            "w",
            encoding="utf-8",
        ) as arquivo:
            json.dump({
                "8421": [{
                    "id": "8421-1",
                    "peca": "Farol esquerdo",
                    "veiculo": "Spin",
                    "ano": "2023",
                    "lado": "esquerdo",
                    "status": "produto_nao_cadastrado",
                    "candidatos": [],
                }]
            }, arquivo)

        resposta = self.client.post(
            "/integracoes/marcelo/pedido-item-foto",
            json={
                "phone": self.phone,
                "item_id": "8421-1",
                "foto": "https://exemplo.com/farol.jpg",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json["cadastro_automatico"]["criado"])
        self.assertEqual(resposta.json["item"]["sku"], "110000")
        self.assertEqual(
            resposta.json["item"]["status"],
            "produto_cadastrado_automaticamente",
        )


if __name__ == "__main__":
    unittest.main()
