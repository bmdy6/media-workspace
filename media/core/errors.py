"""media/core/errors.py - 核心异常定义"""

class MediaError(Exception):
    """Media 模块通用基础异常"""
    def __init__(self, message: str, code: str = "MEDIA_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code

class ConfigError(MediaError):
    """配置读取或密钥缺失异常"""
    def __init__(self, message: str):
        super().__init__(message, code="CONFIG_ERROR")

class StorageError(MediaError):
    """COS 对象存储或文件转存异常"""
    def __init__(self, message: str):
        super().__init__(message, code="STORAGE_ERROR")

class GenerationError(MediaError):
    """生图底层 API 调用或响应解析异常"""
    def __init__(self, message: str):
        super().__init__(message, code="GENERATION_ERROR")
