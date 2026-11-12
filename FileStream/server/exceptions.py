class InvalidHash(Exception):
    def __init__(self, message="Invalid hash"):
        super().__init__(message)
        self.message = message

class FileNotFound(Exception):
    def __init__(self, message="File not found"):
        super().__init__(message)
        self.message = message