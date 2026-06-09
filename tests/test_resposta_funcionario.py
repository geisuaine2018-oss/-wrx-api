import tempfile
import unittest
from unittest.mock import patch

import api_server


class RespostaFake:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data
        self.text = ""

    def json(self):
        return self._data


class RespostaFuncionarioTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.integ_patch = patch.object(api_server, "_INTEG_DIR", self.temp.name)
        self.integ_patch.start()
        self.pedido = {
            "id": 8421,
            "phone": "5521999999999",
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "esquerdo",
            "status": "verificando",
        }

    def tearDown(self):
        self.integ_patch.stop()
        self.temp.cleanup()

    @patch("api_server.requests.patch")
    @patch("api_server.requests.get")
    def test_tenho_coloca_em_atendimento_sem_enviar_cliente(self, get, patch_req):
        get.return_value = RespostaFake(200, [self.pedido])
        patch_req.return_value = RespostaFake(200, [])

        resposta = self.client.post(
            "/integracoes/marcelo/resposta-funcionario",
            json={
                "phone": api_server.FUNCS_PEDIDO["robson"],
                "mensagem": "Tenho #8421-1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["pedido"]["status"], "atendimento")
        self.assertEqual(resposta.json["evento"]["item_id"], "8421-1")
        self.assertFalse(resposta.json["envio_cliente"])
        self.assertEqual(patch_req.call_args.kwargs["json"], {"status": "atendimento"})

    @patch("api_server.requests.patch")
    @patch("api_server.requests.get")
    def test_tenho_busca_produto_existente_e_sugere_sku(self, get, patch_req):
        get.side_effect = [
            RespostaFake(200, [self.pedido]),
            RespostaFake(200, [{
                "sku": "9640",
                "titulo": "Painel frontal Fiat Toro 2016 2021",
                "modelo": "Toro",
                "ano": "2016 a 2021",
                "qtd": 1,
                "preco": 590,
                "fotos": ["https://exemplo.com/painel.jpg"],
                "loc": "A1",
            }]),
        ]
        patch_req.return_value = RespostaFake(200, [])
        self.pedido.update({
            "peca": "Painel frontal diesel",
            "veiculo": "Fiat Toro",
            "ano": "2023",
            "lado": "",
        })

        resposta = self.client.post(
            "/integracoes/marcelo/resposta-funcionario",
            json={
                "phone": api_server.FUNCS_PEDIDO["robson"],
                "mensagem": "Tenho #8421-1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(
            resposta.json["item"]["status"],
            "aguardando_confirmacao_fisica",
        )
        self.assertEqual(resposta.json["item"]["sku_sugerido"], "9640")

    @patch("api_server.requests.patch")
    @patch("api_server.requests.get")
    def test_nao_tenho_mantem_pedido_em_verificacao(self, get, patch_req):
        get.return_value = RespostaFake(200, [self.pedido])
        patch_req.return_value = RespostaFake(200, [])

        resposta = self.client.post(
            "/integracoes/marcelo/resposta-funcionario",
            json={
                "phone": api_server.FUNCS_PEDIDO["rafael"],
                "mensagem": "Não tenho #8421-1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["evento"]["acao"], "nao_tenho")
        self.assertEqual(resposta.json["pedido"]["status"], "verificando")

    @patch("api_server.requests.patch")
    @patch("api_server.requests.get")
    def test_resposta_repetida_nao_atualiza_duas_vezes(self, get, patch_req):
        get.return_value = RespostaFake(200, [self.pedido])
        patch_req.return_value = RespostaFake(200, [])
        payload = {
            "phone": api_server.FUNCS_PEDIDO["geisa"],
            "mensagem": "Tenho #8421-1",
        }

        primeira = self.client.post(
            "/integracoes/marcelo/resposta-funcionario", json=payload
        )
        segunda = self.client.post(
            "/integracoes/marcelo/resposta-funcionario", json=payload
        )

        self.assertFalse(primeira.json["duplicado"])
        self.assertTrue(segunda.json["duplicado"])
        self.assertEqual(patch_req.call_count, 1)

    def test_rejeita_numero_que_nao_e_funcionario(self):
        with patch("api_server.requests.get", return_value=RespostaFake(200, [])):
            resposta = self.client.post(
                "/integracoes/marcelo/resposta-funcionario",
                json={"phone": "5521000000000", "mensagem": "Tenho #8421-1"},
            )

        self.assertEqual(resposta.status_code, 403)


if __name__ == "__main__":
    unittest.main()
