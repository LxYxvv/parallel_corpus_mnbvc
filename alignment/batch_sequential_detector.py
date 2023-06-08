from collections import namedtuple
from typing import Tuple
import itertools
from pathlib import Path
import json

import tiktoken
import pylcs

from text_segmenter import HardLineBreakDetector
import utils

LCSTokenInfo = namedtuple('LCSTokenInfo', ('token', 'length', 'source_line_id'))

class GPTBatchSequentialDetector(HardLineBreakDetector):
    def __init__(self, name, cache_dir, token_limit=1400, use_proxy=False):
        super().__init__(name)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache = {}
        self.token_limit = token_limit
        self.use_proxy = use_proxy
        self.encoder = tiktoken.encoding_for_model("gpt-3.5-turbo")

    @staticmethod
    def tokenize_by_space_splited_word(input_lines: list[str], output_lines: list[str], offset=0) -> Tuple[list[LCSTokenInfo], list[LCSTokenInfo]]:
        """
        Encode `input_lines` and `output_lines` by space splited word as utf-8 single character, to speedup LCS procedure.
        
        Args:
            input_lines (list[str]): The list of lines from source text.
            output_lines (list[str]): The list of lines from the processed text.
            offset (int): utf-8 encoding begin offset for tokenizing.

        Returns:
            list[list[str]]: The batched lines.
        """
        word_dict = {}
        input_tokens_info = []
        output_tokens_info = []
        for input_line_id, input_line in enumerate(input_lines):
            for word in input_line.split():
                input_tokens_info.append(LCSTokenInfo(
                    chr(offset + word_dict.setdefault(word, len(word_dict))),
                    len(word),
                    input_line_id,
                    ))
        
        for output_line_id, output_line in enumerate(output_lines):
            for word in output_line.split():
                if word in word_dict: # 为子序列写的优化
                    output_tokens_info.append(LCSTokenInfo(
                        chr(offset + word_dict[word]),
                        len(word),
                        output_line_id,
                        ))
        return input_tokens_info, output_tokens_info

    @staticmethod
    def lcs_sequence_alignment(input_lines: list[str] | str, output_lines: list[str] | str) -> Tuple[dict[int, set[int]], list[float], list[float]]:
        """
        将input_lines每行的单词用最长公共子序列对齐到output_lines每行的单词中。
        这个函数同时还会计算每行输入输出的单词命中率（此行的已匹配单词总长度/此行单词总长度），
        将命中率低于0.6的输出行连带其对应的输入行扔掉。
        
        Args:
            input_lines(str): 输入的一段话
            output_lines(str): chatgpt给对齐好的一段话
        
        Returns:
            mapping(dict[int, set[int]]): 输出行号对应输入的行号
            irate(list[float]): 输入每行的匹配率（匹配的单词总长度/本行总单词总长度）
            orate(list[float]): 输出每行的匹配率
        """
        if isinstance(input_lines, str):
            input_lines = input_lines.splitlines()
        if isinstance(output_lines, str):
            output_lines = output_lines.splitlines()
        
        # 考虑到运行效率，我们切分粒度采用空格隔开的单词而不是字母
        input_tokens_info, output_tokens_info = GPTBatchSequentialDetector.tokenize_for_lcs(input_lines, output_lines)

        # 算输入输出的每行的单词命中率，即：匹配的单词总字符数 / 单词总字符数
        input_hit_rate = [0 for _ in input_lines] 
        output_hit_rate = [0 for _ in output_lines]

        input_tokens = ''.join(map(lambda x: x[0], input_tokens_info))
        output_tokens = ''.join(map(lambda x: x[0], output_tokens_info))
        # print(f'input_tokens:{len(input_tokens)}, output_tokens:{len(output_tokens)}')
        aligned_indexes = pylcs.lcs_sequence_idx(input_tokens, output_tokens)
        mapping = {}
        for input_token_index, output_token_index in enumerate(aligned_indexes):
            if output_token_index != -1:
                _, input_word_length, input_line_id = input_tokens_info[input_token_index]
                _, output_word_length, output_line_id = output_tokens_info[output_token_index]
                mapping.setdefault(output_line_id, set()).add(input_line_id)
                input_hit_rate[input_line_id] += input_word_length
                output_hit_rate[output_line_id] += output_word_length
        
        for p, i in enumerate(input_hit_rate):
            input_hit_rate[p] /= sum(map(len, input_lines[p].split()))
        for p, i in enumerate(output_hit_rate):
            output_hit_rate[p] /= sum(map(len, output_lines[p].split()))

        # 为了防止加段现象影响准确率，匹配率低于60%的output_line_id直接扔掉
        for p, i in enumerate(output_hit_rate):
            if i < 0.6:
                if p in mapping:
                    mapping.pop(p)

        return mapping, input_hit_rate, output_hit_rate

    def gpt_linebreak_detection_request(self, raw_text: str, record_id: str, batch_index: int) -> str:
        """
        Sends a request to the GPT-3.5 API to detect hard line breaks in the given text.
        Use record_id and batch_index to cache the output.

        Args:
            raw_text (str): The raw text to be processed.
            record_id (int): The unique id of the record.
            batch_index (int): The index of the batch.

        Returns:
            str: The processed text.
        """
        filename = self.cache_dir / f'record_{record_id}_processed_batch_{batch_index}.json'
        if not filename.exists():
            output_text = utils.gpt_detect_hard_line_breaks(raw_text, use_proxy=self.use_proxy)
            with filename.open('w') as f:
                json.dump(output_text, f)
        else:
            with filename.open('r') as f:
                output_text = json.load(f)
        return output_text

    def process_batches(self, batches: list[list[str]], record_id: str) -> Tuple[list[str], list[bool]]:
        """
        Processes each batch of lines by sending them to the GPT-3.5 API and then 
        saving the results to disk and cache.

        Args:
            batches (list[list[str]]): The batched lines to be processed.
            record_id (str): The unique id of the record.

        Returns:
            Tuple[list[str], list[bool]]: The processed lines and their corresponding boolean detections.
        """
        processed_batches = []
        detections = []

        for i, batch in enumerate(batches):
            raw_text = "\n".join(batch)

            output_text = self.gpt_linebreak_detection_request(raw_text, record_id, i)
            # Compare the hard line breaks in the raw text with the output text
            is_hard_line_break = utils.compute_near_linebreak_match(raw_text, output_text, margin=10)

            processed_batches.append(output_text)
            detections.extend(is_hard_line_break)

        return processed_batches, detections

    def gen_batch(self, lines: list[str], begin_lineid: int):
        """从begin_lineid开始拿一个batch"""
        assert begin_lineid < len(lines)
        buf = ''
        for lineid in range(begin_lineid, len(lines)):
            line = lines[lineid]
            tmp = (buf + '\n' if len(buf)>0 else '') + line
            tks = self.encoder.encode(tmp)
            if len(tks) >= self.token_limit:
                return buf, lineid # 本行还没加上，所以是开区间
            buf = tmp
        if buf:
            return buf, lineid + 1

    def detect(self, lines: list[str], record_id: str, **kwargs) -> list[bool]:
        """
        Applies the GPT-3.5 detection technique to the given lines.
        This method first batches the lines, processes the batches, and then post-processes the results.

        Args:
            lines (list[str]): The lines to be detected.
            record_id (int): The unique id of the record. Use to cache the output of GPT

        Returns:
            list[bool]: The detection results.
        """
        # processed_batches = []
        detections = [False] * (len(lines) - 1)

        todo_lineid = 0
        batch_id = 0

        while todo_lineid < len(lines):
            batch, next_line_id = self.gen_batch(lines, todo_lineid) # 利用已有的结果生成input_batch
            if len(self.encoder.encode(batch)) < 20: # 结尾不能成段的噪声可能会让gpt疯狂道歉，这种情况下我们放过
                break

            output_text = self.gpt_linebreak_detection_request(batch, record_id, batch_id)
            # Compare the hard line breaks in the raw text with the output text
            align_map = GPTBatchSequentialDetector.lcs_sequence_alignment(batch, output_text)
            input_line_offset = next_line_id - len(batch.splitlines()) # 第一行在本文件中的下标
            assert lines[input_line_offset] == batch.splitlines()[0]
            if next_line_id < len(lines):
                if len(align_map) > 1:
                    align_map.pop(max(align_map.keys())) # 干掉最后一个分组，避免不完全成段

                todo_lineid = max(itertools.chain(*align_map.values())) + input_line_offset + 1 
            else:
                # 已经做完了本文件，接下来不再请求了
                todo_lineid = len(lines)

            for igroups in align_map.values():
                for igroup in igroups:
                    if igroup + 1 in igroups:
                        detections[igroup + input_line_offset] = True

        return detections


if __name__ == '__main__':
    # Test the GPTBatchSequentialDetector
    detector = GPTBatchSequentialDetector('gpt-remote', "./cache_dir")
    # read val_files 432549.txt
    record_id = '453500'
    with open(f'{record_id}.txt', 'r') as f:
        lines = f.readlines()
    lines = [line.strip() for line in lines]

    post_processed_detections = detector.detect(lines, record_id)
    print(post_processed_detections)
    print(len(post_processed_detections), len(lines))
