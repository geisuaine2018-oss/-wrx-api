// ml_oem_compat.js
// Playwright: busca OEM no Mercado Livre e retorna JSON com anúncios encontrados
// Uso: node ml_oem_compat.js <OEM>
// Saída stdout: JSON array [{titulo, link, preco}, ...]

const { chromium } = require('./pw_driver/package');

const oem = (process.argv[2] || '').trim().toUpperCase();
if (!oem || oem.length < 3) {
    process.stderr.write('Uso: node ml_oem_compat.js <OEM>\n');
    process.exit(1);
}

async function buscarOEM(channel) {
    const opts = {
        headless: true,
        args: [
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage'
        ]
    };
    if (channel) opts.channel = channel;

    const browser = await chromium.launch(opts);
    const ctx = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        locale: 'pt-BR',
        extraHTTPHeaders: { 'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8' }
    });
    const page = await ctx.newPage();

    await page.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
    });

    const oemEnc = encodeURIComponent(oem);
    const searchUrl = `https://lista.mercadolivre.com.br/acessorios-veiculos/${oemEnc}`;

    // Acessa home primeiro para parecer navegação humana
    await page.goto('https://www.mercadolivre.com.br/', { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.waitForTimeout(1200);

    await page.goto(searchUrl, { waitUntil: 'domcontentloaded', timeout: 30000 });

    try {
        await page.waitForSelector(
            'li.ui-search-layout__item, .ui-search-result__wrapper',
            { timeout: 10000 }
        );
    } catch (_) {}

    await page.waitForTimeout(2000);

    const anuncios = await page.evaluate(() => {
        const resultado = [];
        const seletores = [
            'li.ui-search-layout__item',
            '.ui-search-result__wrapper',
            '[data-item-id]'
        ];

        let items = [];
        for (const sel of seletores) {
            items = Array.from(document.querySelectorAll(sel));
            if (items.length > 0) break;
        }

        for (let i = 0; i < Math.min(items.length, 20); i++) {
            const item = items[i];

            const tituloEl = item.querySelector(
                '.ui-search-item__title, h2.ui-search-item__title, .poly-component__title'
            );
            const linkEl = item.querySelector(
                'a.ui-search-item__group__element, a.poly-component__title, a[href*="mercadolivre.com.br/"]'
            );
            const precoEl = item.querySelector(
                '.andes-money-amount__fraction, .price-tag-fraction'
            );

            const titulo = tituloEl ? tituloEl.textContent.trim() : '';
            const link = linkEl ? linkEl.href.split('?')[0] : '';
            const preco = precoEl ? precoEl.textContent.trim().replace(/\D/g, '') : '';

            if (titulo && titulo.length > 5) {
                resultado.push({ titulo, link, preco });
            }
        }
        return resultado;
    });

    await browser.close();
    return anuncios;
}

(async () => {
    for (const channel of ['msedge', null]) {
        try {
            const anuncios = await buscarOEM(channel);
            process.stdout.write(JSON.stringify(anuncios, null, 0));
            process.exit(0);
        } catch (e) {
            process.stderr.write(`Canal ${channel || 'bundled'} falhou: ${e.message}\n`);
            continue;
        }
    }
    // Retorna array vazio se falhar (não quebra o servidor)
    process.stdout.write('[]');
    process.exit(0);
})();
