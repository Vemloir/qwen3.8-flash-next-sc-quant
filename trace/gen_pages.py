"""
Frontend screenshots for the image part of the calibration trace: the local model writes one
self-contained HTML page per spec (the MiMo webdev task prompts plus app-style UIs), and headless
Chromium renders each to a PNG at desktop or phone size. Some specs ask for a page with a layout
bug, since an agent mostly looks at its own frontend to find what is broken.

    PYTHONPATH=<exllamav3 tree> python gen_pages.py -m <model> -mcl 44 --tasks tasks.json -o pages/
"""
import argparse, json, os, re, subprocess, zlib
import torch
from exllamav3 import Generator, Job, model_init
from exllamav3.generator.sampler import ComboSampler

CHROME = os.path.expanduser("~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome")

OWN_SPECS = [
    ("en", "desktop", "An analytics dashboard for a SaaS product: left sidebar navigation, KPI cards, a line chart and a bar chart drawn with inline SVG, and a recent-activity table."),
    ("ru", "desktop", "Админ-панель интернет-магазина: таблица заказов с фильтрами, статусами-бейджами и пагинацией, сверху поиск и кнопка экспорта."),
    ("en", "phone", "A mobile login screen with email and password fields where the password field shows a validation error, a primary button and a 'forgot password' link."),
    ("ru", "desktop", "Страница тарифов: три карточки (Базовый, Про, Команда), переключатель месяц/год, выделенный рекомендуемый тариф и FAQ ниже."),
    ("en", "desktop", "A kanban board with four columns (Backlog, In progress, Review, Done), draggable-looking cards with avatars, tags and due dates."),
    ("en", "phone", "A chat app conversation view: header with contact name and status, message bubbles on both sides with timestamps, an input bar with attachment and send buttons."),
    ("ru", "desktop", "Карточка товара: галерея изображений (цветные плейсхолдеры), цена со скидкой, выбор размера и цвета, кнопка «В корзину», отзывы."),
    ("en", "desktop", "A settings page in dark mode: tabs (Profile, Notifications, Security, Billing), toggles, a danger zone with a delete-account button."),
    ("en", "desktop", "A blog article page with a long title, author byline, table of contents in a sticky sidebar, code blocks and a pull quote."),
    ("ru", "desktop", "Страница 404 с иллюстрацией из CSS-фигур, поиском по сайту и списком популярных разделов."),
    ("en", "desktop", "A calendar month view with colored event chips, a mini calendar and an upcoming-events list on the side."),
    ("en", "desktop", "A file manager: breadcrumb path, grid of folders and files with icons drawn in CSS, a details panel for the selected file."),
    ("en", "desktop", "A landing page whose hero section has a layout bug: the heading overflows its container and overlaps the call-to-action button, and the navigation wraps onto two lines."),
    ("ru", "phone", "Мобильная версия лендинга кофейни, в которой сломана вёрстка: карточки меню вылезают за правый край экрана и появляется горизонтальная прокрутка."),
    ("en", "desktop", "A data table page where the table header is misaligned with the body columns and long cell values are not truncated, breaking the layout."),
    ("en", "desktop", "A checkout form with shipping and payment sections, an order summary sidebar, inline field errors and a disabled submit button."),
    ("ru", "desktop", "Дашборд мониторинга серверов: карточки с загрузкой CPU/RAM в виде кольцевых индикаторов на SVG, таблица алертов, тёмная тема."),
]

PROMPT = ("Write a complete, self-contained single-file HTML page (inline CSS and JS, no external fonts, images, "
          "scripts or CDNs; use CSS shapes, gradients or inline SVG for any imagery) for the following brief. "
          "Use realistic placeholder content in the brief's language. Keep it compact: one screen of content "
          "plus a little below the fold, under 300 lines in total. Output only the HTML in one ```html block.\n\n"
          "Brief:\n{spec}")


@torch.inference_mode()
def main(args):
    os.makedirs(args.output, exist_ok = True)
    specs = []
    for t in json.load(open(args.tasks)):
        if t["domain"] == "webdev" and t["instance_id"] not in {s["id"] for s in specs}:
            spec = t["prompt"] if len(t["prompt"]) < 2500 else t["prompt"][:2500]
            specs.append({"id": t["instance_id"], "lang": t["lang"], "size": "desktop", "spec": spec})
    for k, (lang, size, spec) in enumerate(OWN_SPECS):
        specs.append({"id": f"own_{k:02d}", "lang": lang, "size": size, "spec": spec})

    model, config, cache, tokenizer = model_init.init(args)[:4]
    gen = Generator(model = model, cache = cache, tokenizer = tokenizer)
    sampler = ComboSampler(temperature = 1.0, top_k = 20, top_p = 0.95)
    out = {}
    for i, s in enumerate(specs):
        ids = tokenizer.hf_chat_template([{"role": "user", "content": PROMPT.format(spec = s["spec"])}],
                                         add_generation_prompt = True, enable_thinking = False)
        gen.enqueue(Job(input_ids = ids, max_new_tokens = args.max_new_tokens, sampler = sampler,
                        stop_conditions = config.eos_token_id_list, identifier = i,
                        seed = zlib.crc32(s["id"].encode())))
        out[i] = ""
    while gen.num_remaining_jobs():
        for r in gen.iterate():
            if r["stage"] == "streaming":
                out[r["identifier"]] += r.get("text", "")

    pages = []
    for i, s in enumerate(specs):
        m = re.search(r"```html\s*(.*?)(```|$)", out[i], re.S)
        html = m.group(1) if m else out[i]
        if "</html>" not in html.lower():
            print(f" !! {s['id']}: HTML incomplete (length cap), skipped")
            continue
        hp = os.path.join(args.output, f"{s['id']}.html")
        open(hp, "w").write(html)
        png = os.path.join(args.output, f"{s['id']}.png")
        w, h = (390, 844) if s["size"] == "phone" else (1440, 900)
        # virtual time lets load animations and scripts finish before the capture
        subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--virtual-time-budget=8000", "--run-all-compositor-stages-before-draw",
                        f"--screenshot={png}", f"--window-size={w},{h}", "file://" + os.path.abspath(hp)],
                       capture_output = True, timeout = 120)
        # a near-empty PNG is a blank render
        if os.path.exists(png) and os.path.getsize(png) > 20000:
            pages.append({**s, "html": hp, "file": png})
            print(f" -- {s['id']}: {len(html)} chars -> {png}")
        else:
            print(f" !! {s['id']}: blank render, skipped")
    json.dump(pages, open(os.path.join(args.output, "pages.json"), "w"), ensure_ascii = False, indent = 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    model_init.add_args(ap, default_cache_size = 131072)
    ap.add_argument("--tasks", required = True)
    ap.add_argument("-o", "--output", required = True)
    ap.add_argument("--max_new_tokens", type = int, default = 16384)
    main(ap.parse_args())
