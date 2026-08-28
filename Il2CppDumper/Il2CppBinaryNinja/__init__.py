import os.path
import json
from binaryninja import *


def get_addr(bv: BinaryView, addr: int): return bv.start + addr


class ProgressInfo:
    def __init__(self, task: BackgroundTaskThread, max_steps: int):
        self.task = task
        self.progress_max = 0
        self.progress_curr = 0
        self.max_steps = max_steps
        self.step = 0
        self.step_name = ''


    def set_step(self, step_name: str, progress_max: int) -> bool:
        self.task.logger.log_info(f'start step {step_name}')
        self.step_name = step_name
        self.step += 1
        self.progress_max = progress_max
        self.progress_curr = -1
        return not self.task.cancelled


    def update_progress(self) -> bool:
        if self.task.cancelled or self.step == 0:
            self.task.progress = ''
            return False

        self.progress_curr += 1
        if self.progress_curr % 100 == 0 or self.progress_curr >= self.progress_max:
            percent = (self.progress_curr / self.progress_max) * 100 if self.progress_curr < self.progress_max else 100.0
            self.task.progress = f"Il2Cpp {self.step_name} ({self.step}/{self.max_steps}): {percent:.2f}%"

        return True


class Il2CppProcessTask(BackgroundTaskThread):
    def __init__(self, bv: BinaryView, logger: Logger, script_path: str, header_path: str, load_types: bool, set_string_type: bool, set_metadata_type: bool):
        BackgroundTaskThread.__init__(self, 'Il2Cpp', True)
        self.bv = bv
        self.script_path = script_path
        if header_path:
            self.header_path = header_path
        else:
            self.header_path = os.path.join(os.path.dirname(script_path), 'il2cpp.h')
        self.load_types = load_types and os.path.exists(self.header_path)
        self.set_string_type = set_string_type
        self.set_metadata_type = set_metadata_type
        self.il2cpp_string_type = Type.int(8)
        self.logger = logger


    def process_header(self):
        self.progress_info.set_step('Types', 2)
        self.progress_info.update_progress()

        with open(self.header_path) as f:
            result = self.bv.parse_types_from_string(f.read(), ['--std=c++20'], ['/usr/include/c++/v1'])

        self.progress_info.update_progress()
        type_list = [(Type.generate_auto_type_id('il2cppdumper', name), name, result.types[name]) for name in result.types]

        self.bv.define_types(type_list, progress_func=None)


    def process_methods(self, data: dict):
        scriptMethods = data['ScriptMethod']
        if not self.progress_info.set_step('Methods', len(scriptMethods)):
            return

        for scriptMethod in scriptMethods:
            if not self.progress_info.update_progress():
                return

            addr = get_addr(self.bv, scriptMethod['Address'])
            name = scriptMethod['Name'].replace('$', '_').replace('.', '_')
            signature = scriptMethod['Signature']
            func = self.bv.get_function_at(addr) or self.bv.create_user_function(addr)

            if func == None:
                continue

            if func.name != name:
                func.name = name

            func.add_tag('Il2CppSignature', signature)


    def process_strings(self, data: dict):
        scriptStrings = data['ScriptString']
        if not self.progress_info.set_step('Script Strings', len(scriptStrings)):
            return

        for scriptString in scriptStrings:
            if not self.progress_info.update_progress():
                return

            addr = get_addr(self.bv, scriptString['Address'])
            value = scriptString['Value']
            name = f'StringLiteral_{hex(scriptString['Address'])}'
            self.bv.set_comment_at(addr, value)
            var = self.bv.get_data_var_at(addr)

            if var == None:
                self.bv.define_data_var(addr, self.il2cpp_string_type, name)
                continue

            if self.set_string_type:
                if var.type != self.il2cpp_string_type:
                    var.type = self.il2cpp_string_type

            if var.name != name:
                var.name = name


    def process_metadata(self, data: dict):
        scriptMetadata = data['ScriptMetadata']
        if not self.progress_info.set_step('Script Metadata', len(scriptMetadata)):
            return

        for metadata in scriptMetadata:
            if not self.progress_info.update_progress():
                return

            addr = get_addr(self.bv, metadata['Address'])
            signature = metadata['Signature']
            name = metadata['Name']
            var = self.bv.get_data_var_at(addr)

            if var == None:
                try:
                    self.bv.define_data_var(addr, signature if (self.set_metadata_type and self.load_types) else self.il2cpp_string_type, name)
                except Exception as e:
                    self.logger.log_warn_for_exception(f'failed to set il2cpp metadata type for {name}: {e}')
                    # fallback to int64_t
                    if (self.set_metadata_type and self.load_types) == False:
                        try:
                            self.bv.define_data_var(addr, self.il2cpp_string_type, name)
                        except Exception as e2:
                            self.logger.log_warn_for_exception(f'failed to set il2cpp metadata type for {name} at all: {e2}')
                continue

            if self.set_metadata_type and self.load_types and signature:
                try:
                    var.type = signature
                except Exception as e:
                    self.logger.log_warn_for_exception(f'failed to set il2cpp metadata type {signature} for {name}: {e}')

            if var.name != name:
                var.name = name


    def run(self):
        try:
            self.bv.set_analysis_hold(True)
            self.bv.begin_undo_actions()

            self.logger.log_info('begin load')
            with open(self.script_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            steps = 0
            if self.load_types: steps += 1
            if 'ScriptMethod' in data: steps += 1
            if 'ScriptString' in data: steps += 1
            if 'ScriptMetadata' in data: steps += 1
            self.logger.log_info(f'total steps: {steps}')

            self.progress_info = ProgressInfo(self, steps)

            if self.load_types: self.process_header()
            if 'ScriptMethod' in data: self.process_methods(data)
            if 'ScriptString' in data: self.process_strings(data)
            if 'ScriptMetadata' in data: self.process_metadata(data)
        except Exception as e:
            self.logger.log_error_for_exception(f'failed to import data: {e}')
        finally:
            self.bv.commit_undo_actions()
            self.bv.update_analysis()


    @staticmethod
    def process(bv: BinaryView):
        bv.create_tag_type('Il2CppSignature', 'IL')
        logger = bv.create_logger('Il2Cpp')

        warning_label = [LabelField('Analysis is ongoing.'), LabelField('Binary Ninja may crash or fail to properly apply all data.')]
        script_dialog = OpenFileNameField('Select script.json', 'script.json', 'script.json')
        note_label = LabelField('Will use il2cpp.h next to script.json if unspecified.')
        header_dialog = OpenFileNameField('Select il2cpp.h', 'il2cpp', '')
        type_checkbox = CheckboxField('Import types', True)
        string_checkbox = CheckboxField('Set StringLiteral types to int64_t', True)
        metadata_checkbox = CheckboxField('Set TypeInfo variable types (slow)', True)

        inputs = []
        if bv.analysis_state > AnalysisState.HoldState:
            inputs += warning_label
        inputs += [script_dialog, header_dialog, note_label, type_checkbox, string_checkbox, metadata_checkbox]
        if not get_form_input(inputs, 'Il2Cpp Import'):
            return

        if not os.path.exists(script_dialog.result):
            return logger.log_error('File not found, try again!')

        task = Il2CppProcessTask(bv, logger, script_dialog.result, header_dialog.result, type_checkbox.result, string_checkbox.result, metadata_checkbox.result)
        task.start()


    @staticmethod
    def process_func(bv: BinaryView, func):
        tags = func.get_function_tags(tag_type='Il2CppSignature')
        if not tags:
            return

        func.type = tags[0].data


    @staticmethod
    def check_func(bv: BinaryView, func):
        return len(func.get_function_tags(tag_type='Il2CppSignature')) > 0

PluginCommand.register('Il2Cpp', 'Process file', Il2CppProcessTask.process)
PluginCommand.register_for_function('Annotate Il2Cpp Signature', 'Annotate function call', Il2CppProcessTask.process_func, is_valid=Il2CppProcessTask.check_func)
