import argparse
import json
import os
import re
import tempfile
import logging
logging.getLogger('numba').setLevel(logging.WARNING)
import librosa
import numpy as np
import torch
from torch import no_grad, LongTensor
import commons
import utils
import gradio as gr
import ONNXVITS_infer
from text import text_to_sequence, _clean_text
from mel_processing import spectrogram_torch

language_marks = {
    "Japanese": "",
    "中文": "[ZH]",
    "日语": "[JA]",
    "英语": "[EN]",
    "Mix": "",
}

limitation = os.getenv("SYSTEM") == "spaces"  # limit text and audio length in huggingface spaces


def create_tts_fn(model, hps, speaker_ids):
    def tts_fn(text, speaker, language, speed, is_symbol):
        if limitation:
            text_len = len(re.sub("\[([A-Z]{2})\]", "", text))
            max_len = 150
            if is_symbol:
                max_len *= 3
            if text_len > max_len:
                return "Error: Text is too long", None
        if language is not None:
            text = language_marks[language] + text + language_marks[language]
        speaker_id = speaker_ids[speaker]
        stn_tst = get_text(text, hps, is_symbol)
        with no_grad():
            x_tst = stn_tst.unsqueeze(0)
            x_tst_lengths = LongTensor([stn_tst.size(0)])
            sid = LongTensor([speaker_id])
            audio = model.infer(x_tst, x_tst_lengths, sid=sid, noise_scale=.667, noise_scale_w=0.8,
                                length_scale=1.0 / speed)[0][0, 0].data.cpu().float().numpy()
        del stn_tst, x_tst, x_tst_lengths, sid
        return "Success", (hps.data.sampling_rate, audio)

    return tts_fn


def create_vc_fn(model, hps, speaker_ids):
    def vc_fn(original_speaker, target_speaker, input_audio):
        if input_audio is None:
            return "You need to upload an audio", None
        sampling_rate, audio = input_audio
        duration = audio.shape[0] / sampling_rate
        if limitation and duration > 30:
            return "Error: Audio is too long", None
        original_speaker_id = speaker_ids[original_speaker]
        target_speaker_id = speaker_ids[target_speaker]

        audio = (audio / np.iinfo(audio.dtype).max).astype(np.float32)
        if len(audio.shape) > 1:
            audio = librosa.to_mono(audio.transpose(1, 0))
        if sampling_rate != hps.data.sampling_rate:
            audio = librosa.resample(audio, orig_sr=sampling_rate, target_sr=hps.data.sampling_rate)
        with no_grad():
            y = torch.FloatTensor(audio)
            y = y.unsqueeze(0)
            spec = spectrogram_torch(y, hps.data.filter_length,
                                     hps.data.sampling_rate, hps.data.hop_length, hps.data.win_length,
                                     center=False)
            spec_lengths = LongTensor([spec.size(-1)])
            sid_src = LongTensor([original_speaker_id])
            sid_tgt = LongTensor([target_speaker_id])
            audio = model.voice_conversion(spec, spec_lengths, sid_src=sid_src, sid_tgt=sid_tgt)[0][
                0, 0].data.cpu().float().numpy()
        del y, spec, spec_lengths, sid_src, sid_tgt
        return "Success", (hps.data.sampling_rate, audio)

    return vc_fn


def get_text(text, hps, is_symbol):
    text_norm = text_to_sequence(text, hps.symbols, [] if is_symbol else hps.data.text_cleaners)
    if hps.data.add_blank:
        text_norm = commons.intersperse(text_norm, 0)
    text_norm = LongTensor(text_norm)
    return text_norm


def create_to_symbol_fn(hps):
    def to_symbol_fn(is_symbol_input, input_text, temp_text):
        return (_clean_text(input_text, hps.data.text_cleaners), input_text) if is_symbol_input \
            else (temp_text, temp_text)

    return to_symbol_fn


models_tts = []
models_vc = []
models_info = [
    {
        "title": "语音生成(Trilingual)",
        "languages": [ '中文','日语', '英语', 'Mix'],
        "description": "",
        "model_path": "./pretrained_models/G_trilingual.pth",
        "config_path": "./configs/trained-speaker.json",
        "examples": [['你好，训练员先生，很高兴见到你。', '派蒙', '中文', 1, False],
                     ['To be honest, I have no idea what to say as examples.', '派蒙', '英语',
                      1, False],
                     ['授業中に出しだら，学校生活終わるですわ。', '綾地', '日语', 1, False],
                     ['[JA]こんにちわ。[JA][ZH]你好！[ZH][EN]Hello![EN]', '綾地', 'Mix', 1, False]],
        "onnx_dir": "./ONNX_net/G_trilingual/"
    },
    {
        "title": "日语",
        "languages": ["日语"],
        "description": "Build By 清风",
        "model_path": "./pretrained_models/G_jp.pth",
        "config_path": "./configs/uma87.json",
        "examples": [['お疲れ様です，トレーナーさん。', '无声铃鹿 Silence Suzuka (Umamusume Pretty Derby)', 'Japanese', 1, False],
                     ['張り切っていこう！', '北部玄驹 Kitasan Black (Umamusume Pretty Derby)', 'Japanese', 1, False],
                     ['何でこんなに慣れでんのよ，私のほが先に好きだっだのに。', '草上飞 Grass Wonder (Umamusume Pretty Derby)', 'Japanese', 1, False],
                     ['授業中に出しだら，学校生活終わるですわ。', '目白麦昆 Mejiro Mcqueen (Umamusume Pretty Derby)', 'Japanese', 1, False],
                     ['お帰りなさい，お兄様！', '米浴 Rice Shower (Umamusume Pretty Derby)', 'Japanese', 1, False],
                     ['私の処女をもらっでください！', '米浴 Rice Shower (Umamusume Pretty Derby)', 'Japanese', 1, False]],
        "onnx_dir": "./ONNX_net/G_jp/"
    }
]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", action="store_true", default=False, help="是否开放公共链接")
    parser.add_argument("--port",default=7860,help="端口,默认7860")
    parser.add_argument("--browser",default=True,help="自动打开浏览器")
    args = parser.parse_args()
    for info in models_info:
        name = info['title']
        lang = info['languages']
        examples = info['examples']
        config_path = info['config_path']
        model_path = info['model_path']
        description = info['description']
        onnx_dir = info["onnx_dir"]
        hps = utils.get_hparams_from_file(config_path)
        model = ONNXVITS_infer.SynthesizerTrn(
            len(hps.symbols),
            hps.data.filter_length // 2 + 1,
            hps.train.segment_size // hps.data.hop_length,
            n_speakers=hps.data.n_speakers,
            ONNX_dir=onnx_dir,
            **hps.model)
        utils.load_checkpoint(model_path, model, None)
        model.eval()
        speaker_ids = hps.speakers
        speakers = list(hps.speakers.keys())
        models_tts.append((name, description, speakers, lang, examples,
                           hps.symbols, create_tts_fn(model, hps, speaker_ids),
                           create_to_symbol_fn(hps)))
        models_vc.append((name, description, speakers, create_vc_fn(model, hps, speaker_ids)))
    app = gr.Blocks()
    with app:
        with gr.Tabs():
            with gr.TabItem("TTS"):
                with gr.Tabs():
                    for i, (name, description, speakers, lang, example, symbols, tts_fn, to_symbol_fn) in enumerate(
                            models_tts):
                        with gr.TabItem(name):
                            gr.Markdown(description)
                            with gr.Row():
                                with gr.Column():
                                    textbox = gr.TextArea(label="文本(Text)",
                                                          placeholder="Type your sentence here(Maximum 150 words)",
                                                          value="早上好", elem_id=f"tts-input")
                                    with gr.Accordion(label="音素输入(Phoneme Input)", open=False):
                                        temp_text_var = gr.Variable()
                                        symbol_input = gr.Checkbox(value=False, label="Symbol input")
                                        symbol_list = gr.Dataset(label="特征列表(Symbol list)", components=[textbox],
                                                                 samples=[[x] for x in symbols],
                                                                 elem_id=f"symbol-list")
                                        symbol_list_json = gr.Json(value=symbols, visible=False)
                                    symbol_input.change(to_symbol_fn,
                                                        [symbol_input, textbox, temp_text_var],
                                                        [textbox, temp_text_var])
                                    symbol_list.click(None, [symbol_list, symbol_list_json], textbox,
                                                      _js=f"""
                                    (i, symbols, text) => {{
                                        let root = document.querySelector("body > gradio-app");
                                        if (root.shadowRoot != null)
                                            root = root.shadowRoot;
                                        let text_input = root.querySelector("#tts-input").querySelector("textarea");
                                        let startPos = text_input.selectionStart;
                                        let endPos = text_input.selectionEnd;
                                        let oldTxt = text_input.value;
                                        let result = oldTxt.substring(0, startPos) + symbols[i] + oldTxt.substring(endPos);
                                        text_input.value = result;
                                        let x = window.scrollX, y = window.scrollY;
                                        text_input.focus();
                                        text_input.selectionStart = startPos + symbols[i].length;
                                        text_input.selectionEnd = startPos + symbols[i].length;
                                        text_input.blur();
                                        window.scrollTo(x, y);

                                        text = text_input.value;

                                        return text;
                                    }}""")
                                    # select character
                                    char_dropdown = gr.Dropdown(choices=speakers, value=speakers[0], label='原型(character)')
                                    language_dropdown = gr.Dropdown(choices=lang, value=lang[0], label='语言(language)')
                                    duration_slider = gr.Slider(minimum=0.1, maximum=5, value=1, step=0.1,
                                                                label='速度(Speed)')
                                with gr.Column():
                                    text_output = gr.Textbox(label="消息(Message)")
                                    audio_output = gr.Audio(label="输出音频(Output Audio)", elem_id="tts-audio")
                                    # btn = gr.Button("Generate!")
                                    btn = gr.Button("生成(Generate)")
                                    btn.click(tts_fn,
                                              inputs=[textbox, char_dropdown, language_dropdown, duration_slider,
                                                      symbol_input],
                                              outputs=[text_output, audio_output])
                            gr.Examples(
                                examples=example,
                                inputs=[textbox, char_dropdown, language_dropdown,
                                        duration_slider, symbol_input],
                                outputs=[text_output, audio_output],
                                fn=tts_fn
                            )
    app.queue(concurrency_count=3).launch(show_api=False, share=args.share,server_port=args.port,inbrowser=args.browser)
