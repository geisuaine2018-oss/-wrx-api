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

    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_processa_tenho_com_palavra_codigo_sem_cerquilha(self, get, post):
        estado = os.path.join(self.temp.name, "mensagens_func_processadas.json")
        with open(estado, "w", encoding="utf-8") as arquivo:
            json.dump([], arquivo)
        get.return_value = RespostaFake(200, [{
            "id": 14,
            "numero": api_server.FUNCS_PEDIDO["rafael"],
            "mensagem": "Tenho codigo 8421-2",
            "de_mim": False,
            "criado_em": "2026-06-09T10:04:00Z",
        }])
        post.return_value = RespostaFake(200, {"ok": True})

        resposta = self.client.post(
            "/integracoes/whatsapp/processar-respostas-funcionarios"
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["processadas"], 1)
        self.assertEqual(post.call_args.kwargs["json"]["pedido_id"], "8421")
        self.assertEqual(post.call_args.kwargs["json"]["item_id"], "8421-2")

    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_vincula_foto_enviada_depois_do_tenho(self, get, post):
        estado = os.path.join(self.temp.name, "mensagens_func_processadas.json")
        with open(estado, "w", encoding="utf-8") as arquivo:
            json.dump([], arquivo)
        phone = api_server.FUNCS_PEDIDO["robson"]
        with open(
            os.path.join(self.temp.name, "respostas_func.json"),
            "w",
            encoding="utf-8",
        ) as arquivo:
            json.dump({
                f"271-1:{phone}:tenho": {
                    "item_id": "271-1",
                    "acao": "tenho",
                    "whatsapp": phone,
                    "recebido_em": "2026-06-09T10:00:00Z",
                }
            }, arquivo)
        get.return_value = RespostaFake(200, [{
            "id": 12,
            "numero": phone,
            "mensagem": "[imagem]",
            "tipo": "image",
            "media_url": "https://exemplo.com/painel.jpg",
            "de_mim": False,
            "criado_em": "2026-06-09T10:01:00Z",
        }])
        post.return_value = RespostaFake(200, {"ok": True})

        resposta = self.client.post(
            "/integracoes/whatsapp/processar-respostas-funcionarios"
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["fotos_vinculadas"], 1)
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "phone": phone,
                "item_id": "271-1",
                "foto": "https://exemplo.com/painel.jpg",
            },
        )

    @patch("api_server._waha_enviar")
    @patch("api_server.requests.post")
    @patch("api_server.requests.get")
    def test_codigo_invalido_nao_e_marcado_como_processado(
        self, get, post, enviar
    ):
        estado = os.path.join(self.temp.name, "mensagens_func_processadas.json")
        with open(estado, "w", encoding="utf-8") as arquivo:
            json.dump([], arquivo)
        get.return_value = RespostaFake(200, [{
            "id": 13,
            "numero": api_server.FUNCS_PEDIDO["robson"],
            "mensagem": "Tenho #268-1",
            "de_mim": False,
            "criado_em": "2026-06-09T10:03:00Z",
        }])
        post.return_value = RespostaFake(404, {"erro": "pedido nao encontrado"})

        resposta = self.client.post(
            "/integracoes/whatsapp/processar-respostas-funcionarios"
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json["processadas"], 0)
        with open(estado, encoding="utf-8") as arquivo:
            self.assertNotIn("13", json.load(arquivo))
        enviar.assert_called_once()


if __name__ == "__main__":
    unittest.main()
