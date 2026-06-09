import json
import os
import tempfile
import unittest
from unittest.mock import patch

import api_server


class RespostaFake:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data
        self.text = ""

    def json(self):
        return self._data


class DisparoPedidosFuncionariosTest(unittest.TestCase):
    def setUp(self):
        self.client = api_server.app.test_client()
        self.temp = tempfile.TemporaryDirectory()
        self.integ_patch = patch.object(api_server, "_INTEG_DIR", self.temp.name)
        self.integ_patch.start()
        with open(
            os.path.join(self.temp.name, "avisos_func.json"),
            "w",
            encoding="utf-8",
        ) as arquivo:
            json.dump({}, arquivo)

    def tearDown(self):
        self.integ_patch.stop()
        self.temp.cleanup()

    @patch("api_server._func_em_janela", return_value=True)
    @patch("api_server._waha_enviar", return_value=(True, None))
    @patch("api_server.requests.patch", return_value=RespostaFake(204, []))
    @patch("api_server.requests.get")
    def test_mesmo_pedido_nao_dispara_duas_vezes(
        self, get, patch_req, enviar, _janela
    ):
        pedido = {
            "id": 8421,
            "phone": "5521999999999",
            "peca": "Farol esquerdo",
            "veiculo": "Spin",
            "ano": "2023",
            "lado": "esquerdo",
        }
        get.return_value = RespostaFake(200, [pedido])

        primeira = self.client.post("/integracoes/whatsapp/pedidos-manha")
        segunda = self.client.post("/integracoes/whatsapp/pedidos-manha")

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(primeira.json["pedidos_disparados"], 1)
        self.assertEqual(segunda.json["pedidos_disparados"], 0)
        self.assertEqual(enviar.call_count, len(api_server.FUNCS_PEDIDO))
        self.assertIn("status=eq.aguardando", get.call_args_list[0].args[0])
        patch_req.assert_called_once()
        self.assertEqual(
            patch_req.call_args.kwargs["params"],
            {"id": "eq.8421", "status": "eq.aguardando"},
        )
        self.assertEqual(
            patch_req.call_args.kwargs["json"],
            {"status": "verificando"},
        )


if __name__ == "__main__":
    unittest.main()
