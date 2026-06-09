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
    def test_descarta_outro_tipo_e_sinaliza_ano_incompativel(self, get):
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

        self.assertTrue(resposta.json["encontrado"])
        self.assertEqual(resposta.json["candidatos"][0]["sku"], "22")
        self.assertFalse(resposta.json["candidatos"][0]["ano_compativel"])

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

    @patch("api_server.requests.get")
    def test_tolera_erro_de_digitacao_paniel(self, get):
        get.return_value = RespostaFake(200, [{
            "sku": "9640",
            "titulo": "Painel frontal Fiat Toro 2016 2021",
            "modelo": "Toro",
            "ano": "2016 a 2021",
            "qtd": 1,
            "preco": 590,
        }])

        resposta = self.client.post("/integracoes/marcelo/buscar-estoque", json={
            "peca": "paniel frontal diesel",
            "veiculo": "fiat toro 2023fiat",
            "ano": "2023fiat",
        })

        self.assertTrue(resposta.json["encontrado"])
        self.assertEqual(resposta.json["candidatos"][0]["sku"], "9640")
        self.assertFalse(resposta.json["candidatos"][0]["ano_compativel"])

    @patch("api_server.requests.get")
    def test_porta_duster_nao_aceita_outro_modelo_nem_acessorio(self, get):
        get.return_value = RespostaFake(200, [
            {
                "sku": "108186",
                "titulo": "Porta traseira direita Renault Logan 2014 2025",
                "marca": "Renault",
                "modelo": "Logan",
                "qtd": 1,
            },
            {
                "sku": "10508",
                "titulo": "Fechadura traseira direita Peugeot 208",
                "marca": "Peugeot",
                "modelo": "208",
                "qtd": 2,
            },
            {
                "sku": "108229",
                "titulo": "Limitador porta traseira direita Chevrolet Onix",
                "marca": "Chevrolet",
                "modelo": "Onix",
                "qtd": 2,
            },
            {
                "sku": "200001",
                "titulo": "Porta dianteira direita Renault Duster 2015",
                "marca": "Renault",
                "modelo": "Duster",
                "ano": "2015",
                "qtd": 1,
            },
        ])

        resposta = self.client.post("/integracoes/marcelo/buscar-estoque", json={
            "peca": "2 porta dianteira direita e traseira",
            "veiculo": "Renault Duster 2015",
            "ano": "2015",
        })

        self.assertEqual(
            [item["sku"] for item in resposta.json["candidatos"]],
            ["200001"],
        )

    @patch("api_server.requests.get")
    def test_peugeot_208_direita_nao_aceita_2008_nem_esquerda(self, get):
        get.return_value = RespostaFake(200, [
            {
                "sku": "6382",
                "titulo": "Lanterna traseira direita Peugeot 2008",
                "marca": "Peugeot",
                "modelo": "2008",
                "ano": "2022",
                "qtd": 1,
            },
            {
                "sku": "10460",
                "titulo": "Lanterna traseira esquerda Peugeot 208",
                "marca": "Peugeot",
                "modelo": "208",
                "ano": "2015",
                "lado": "left",
                "qtd": 1,
            },
            {
                "sku": "8347",
                "titulo": "Lanterna traseira direita Peugeot 208 2013 a 20",
                "marca": "Peugeot",
                "modelo": "208",
                "ano": "2013",
                "qtd": 1,
                "preco": 250,
            },
        ])

        resposta = self.client.post("/integracoes/marcelo/buscar-estoque", json={
            "peca": "Lanterna traseira direita",
            "veiculo": "Peugeot 208",
            "ano": "2013 a 2020",
        })

        self.assertEqual(
            [item["sku"] for item in resposta.json["candidatos"]],
            ["8347"],
        )


if __name__ == "__main__":
    unittest.main()
