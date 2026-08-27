from binaryninja import *
from os.path import exists

def get_addr(bv: BinaryView, addr: int):
    imageBase = bv.start
    return imageBase + addr

class Il2CppProcessTask(BackgroundTaskThread):
    def __init__(self, bv: BinaryView, script_path: str,
                 header_path: str):
        BackgroundTaskThread.__init__(self, "Il2Cpp start", True)
        self.bv = bv
        self.script_path = script_path
        self.header_path = header_path
    
    def process_header(self):
        self.progress = "Il2Cpp types (1/3)"
        with open(self.header_path) as f:
            result = self.bv.parse_types_from_string(f.read(), ["--std=c++20"], ["/usr/include/c++/v1"])
        type_list = [(Type.generate_auto_type_id("il2cppdumper", name), name, result.types[name]) for name in result.types]
        self.bv.define_types(type_list, progress_func=None)
    
    def process_methods(self, data: dict):
        self.progress = f"Il2Cpp methods (2/3)"
        scriptMethods = data["ScriptMethod"]
        length = len(scriptMethods)
        i = 0
        for scriptMethod in scriptMethods:
            if self.cancelled:
                self.progress = "Il2Cpp cancelled, aborting"
                return
            i += 1
            if i % 100 == 0:
                percent = i / length * 100
                self.progress = f"Il2Cpp methods: {percent:.2f}%"
            addr = get_addr(self.bv, scriptMethod["Address"])
            name = scriptMethod["Name"]
            signature = scriptMethod["Signature"]
            func = self.bv.get_function_at(addr) or self.bv.create_user_function(addr)
            if func == None:
                continue
            func.name = name.replace("$", "_").replace(".", "_")
            func.add_tag('Il2CppSignature', signature, auto=True)
        
    def process_strings(self, data: dict):
        self.progress = "Il2Cpp strings (3/3)"
        scriptStrings = data["ScriptString"]
        i = 0
        for scriptString in scriptStrings:
            i += 1
            if self.cancelled:
                self.progress = "Il2Cpp cancelled, aborting"
                return
            addr = get_addr(self.bv, scriptString["Address"])
            value = scriptString["Value"]
            self.bv.set_comment_at(addr, value)
            var = self.bv.get_data_var_at(addr) or self.bv.define_data_var(addr, "int64_t")
            if var == None:
                continue
            var.name = f"StringLiteral_{i}"

    def run(self):
        if exists(self.header_path):
            self.process_header()
        else:
            log_warn("Header file not found")
        data = json.loads(open(self.script_path, 'rb').read().decode('utf-8'))
        if "ScriptMethod" in data:
            self.process_methods(data)
        if "ScriptString" in data:
            self.process_strings(data)

def process(bv: BinaryView):
    if bv.get_tag_type('Il2CppSignature') == None:
        bv.create_tag_type('Il2CppSignature', '📜')

    scriptDialog = OpenFileNameField("Select script.json", "script.json", "script.json")
    headerDialog = OpenFileNameField("Select il2cpp.h", "il2cpp.h", "il2cpp.h")
    if not get_form_input([scriptDialog, headerDialog], "script.json from Il2CppDumper"):
        return log_error("File not selected, try again!")
    if not exists(scriptDialog.result):
        return log_error("File not found, try again!")
    task = Il2CppProcessTask(bv, scriptDialog.result, headerDialog.result)
    task.start()

def process_func(bv: BinaryView, func):
    tags = func.get_function_tags(auto=True, tag_type="Il2CppSignature")
    if not tags:
        return

    func.type = tags[0].data

def check_func(bv: BinaryView, func):
    return len(func.get_function_tags(auto=True, tag_type="Il2CppSignature")) > 0

PluginCommand.register("Il2Cpp", "Process file", process)
PluginCommand.register_for_function("Annotate Il2Cpp Signature", "Annotate function call", process_func, is_valid=check_func)
