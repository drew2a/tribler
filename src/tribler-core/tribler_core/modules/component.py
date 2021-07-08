import logging


class Component:
    def __init__(self, *args, **kwargs):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info('Init')

    async def run(self, mediator):
        self.logger.info('Run')

    async def shutdown(self, mediator):
        self.logger.info('Shutdown')