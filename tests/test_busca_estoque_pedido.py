import unittest
from unittest.mock import patch

import api_server


class RespostaFake:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class BuscaEstoquePedidoTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()

    @patch("api_server.requests.get")
    def test_encontra_candidato_sem_alterar_estoque(self, get):
        get.return_value = RespostaFake(200, [{
            "sku": "109095",
            "titulo": "Farol esquerdo Chevrolet Spin",
            "descricao": "",
            "categoria": "Iluminacao",
            "marca": "Chevrolet",
            "modelo": "Spin",
            "ano": "2023",
            "lado": "Esquerdo",
            "compatibilidade": [],
            "preco": 650,
            "qtd": 1,
            "fotos": ["https://exemplo/farol.jpg"],
            "loc": "A1",
            "atualizado": "2026-06-01T10:00:00Z",
            "cadastrado_em": "2026-05-01T10:00:00Z",
        }])

        resposta = self.client.post("/integracoes/marcelo/buscar-estoque", json={
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "esquerdo",
        })

        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(resposta.json["encontrado"])
        self.assertTrue(resposta.json["confirmacao_fisica_obrigatoria"])
        self.assertEqual(
            resposta.json["status_sugerido"],
            "aguardando_confirmacao_fisica",
        )
        self.assertEqual(resposta.json["candidatos"][0]["sku"], "109095")
        get.assert_called_once()

    @patch("api_server.requests.get")
    def test_informa_produto_nao_cadastrado(self, get):
        get.return_value = RespostaFake(200, [])

        resposta = self.client.post("/integracoes/marcelo/buscar-estoque", json={
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
        })

        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.json["encontrado"])
        self.assertEqual(
            resposta.json["status_sugerido"],
            "produto_nao_cadastrado",
        )

    @patch("api_server.requests.get")
    def test_descarta_peca_de_outro_veiculo(self, get):
        get.return_value = RespostaFake(200, [{
            "sku": "20",
            "titulo": "Farol esquerdo Fiat Pulse 2023",
            "modelo": "Pulse",
            "ano": "2023",
            "lado": "Esquerdo",
            "qtd": 1,
            "atualizado": "2026-06-01T10:00:00Z",
        }])

        resposta = self.client.post("/integracoes/marcelo/buscar-estoque", json={
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
        })

        self.assertFalse(resposta.json["encontrado"])
        self.assertEqual(resposta.json["candidatos"], [])

    @patch("api_server.requests.get")
    def test_descarta_outro_tipo_de_peca_e_ano_incompativel(self, get):
        get.return_value = RespostaFake(200, [
            {
                "sku": "21",
                "titulo": "Paralama esquerdo Chevrolet Spin 2023",
                "modelo": "Spin",
                "ano": "2023",
                "qtd": 1,
            },
            {
                "sku": "22",
                "titulo": "Farol esquerdo Chevrolet Spin 2012 a 2019",
                "modelo": "Spin",
                "ano": "2012 a 2019",
                "qtd": 1,
            },
        ])

        resposta = self.client.post("/integracoes/marcelo/buscar-estoque", json={
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
        })

        self.assertFalse(resposta.json["encontrado"])
        self.assertEqual(resposta.json["candidatos"], [])

    @patch("api_server.requests.get")
    def test_marca_confirmacao_vencida_depois_de_90_dias(self, get):
        get.return_value = RespostaFake(200, [{
            "sku": "10",
            "titulo": "Farol esquerdo Spin",
            "modelo": "Spin",
            "ano": "2023",
            "qtd": 1,
            "atualizado": "2025-01-01T10:00:00Z",
        }])

        resposta = self.client.post("/integracoes/marcelo/buscar-estoque", json={
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
        })

        self.assertTrue(resposta.json["candidatos"][0]["confirmacao_vencida"])


if __name__ == "__main__":
    unittest.main()
