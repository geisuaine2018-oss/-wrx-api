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


class ConferenciaEnvioClienteTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.integ_patch = patch.object(api_server, "_INTEG_DIR", self.temp.name)
        self.integ_patch.start()
        self.phone_func = api_server.FUNCS_PEDIDO["robson"]
        self.pedido = {
            "id": 8421,
            "phone": "5521999999999",
            "nome": "Maria Cliente",
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "esquerdo",
            "status": "atendimento",
            "criado_em": "2026-06-09T10:00:00Z",
        }

    def tearDown(self):
        self.integ_patch.stop()
        self.temp.cleanup()

    def _salvar_item_confirmado(self):
        with open(
            os.path.join(self.temp.name, "pedido_itens.json"),
            "w",
            encoding="utf-8",
        ) as arquivo:
            json.dump({"8421": [{
                "id": "8421-1",
                "pedido_id": "8421",
                "peca": "Farol esquerdo",
                "veiculo": "Spin",
                "ano": "2023",
                "lado": "esquerdo",
                "sku": "109095",
                "status": "estoque_confirmado",
                "fotos": ["https://exemplo.com/farol.jpg"],
            }]}, arquivo)

    @patch("api_server.requests.patch")
    @patch("api_server.requests.get")
    def test_confirma_sku_compativel_sem_enviar_cliente(self, get, patch_req):
        get.side_effect = [
            RespostaFake(200, [self.pedido]),
            RespostaFake(200, [{
                "sku": "109095",
                "titulo": "Farol esquerdo Chevrolet Spin 2023",
                "modelo": "Spin",
                "ano": "2023",
                "lado": "esquerdo",
                "preco": 650,
                "qtd": 1,
                "fotos": ["https://exemplo.com/farol.jpg"],
            }]),
        ]
        patch_req.return_value = RespostaFake(204, [])

        resposta = self.client.post(
            "/integracoes/marcelo/confirmar-estoque-item",
            json={
                "phone": self.phone_func,
                "item_id": "8421-1",
                "sku": "109095",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["item"]["status"], "estoque_confirmado")
        self.assertFalse(resposta.json["envio_cliente"])

    @patch("api_server.requests.get")
    def test_conferencia_bloqueia_produto_sem_preco(self, get):
        self._salvar_item_confirmado()
        get.side_effect = [
            RespostaFake(200, [self.pedido]),
            RespostaFake(200, [{
                "sku": "109095",
                "titulo": "Farol esquerdo Spin",
                "preco": 0,
                "qtd": 1,
                "fotos": ["https://exemplo.com/farol.jpg"],
            }]),
        ]

        resposta = self.client.post(
            "/integracoes/marcelo/conferencia-final",
            json={"item_id": "8421-1"},
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.json["pronto"])
        self.assertFalse(resposta.json["checks"]["preco_informado"])
        self.assertFalse(resposta.json["envio_cliente"])

    @patch("api_server._waha_enviar_imagem", return_value=(True, ""))
    @patch("api_server._waha_enviar", return_value=(True, ""))
    @patch("api_server.requests.patch")
    @patch("api_server.requests.get")
    def test_envia_uma_unica_vez_apos_confirmacao(
        self, get, patch_req, enviar_texto, enviar_imagem
    ):
        self._salvar_item_confirmado()
        produto = [{
            "sku": "109095",
            "titulo": "Farol esquerdo Spin",
            "preco": 650,
            "qtd": 1,
            "fotos": ["https://exemplo.com/farol.jpg"],
        }]
        get.side_effect = [
            RespostaFake(200, [self.pedido]),
            RespostaFake(200, produto),
        ]
        patch_req.return_value = RespostaFake(204, [])

        primeira = self.client.post(
            "/integracoes/marcelo/enviar-oferta-cliente",
            json={"item_id": "8421-1", "confirmar": True},
        )
        segunda = self.client.post(
            "/integracoes/marcelo/enviar-oferta-cliente",
            json={"item_id": "8421-1", "confirmar": True},
        )

        self.assertEqual(primeira.status_code, 200)
        self.assertFalse(primeira.json["duplicado"])
        self.assertTrue(segunda.json["duplicado"])
        enviar_texto.assert_called_once()
        enviar_imagem.assert_called_once()
        self.assertEqual(patch_req.call_args.kwargs["json"]["status"], "confirmado")

    def test_exige_confirmacao_explicita(self):
        resposta = self.client.post(
            "/integracoes/marcelo/enviar-oferta-cliente",
            json={"item_id": "8421-1"},
        )
        self.assertEqual(resposta.status_code, 400)


if __name__ == "__main__":
    unittest.main()
