import logging
import time
from pathlib import Path
from src.services.execution import claim, execute, materialize_due
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def main():
    while True:
        try:
            materialize_due()
            lease = claim()
            heartbeat.touch()
            if lease:
                # 排班推算按检查点分批；还有单元待算时立即续领，不必等待唤醒。
                more = execute(lease)
                heartbeat.touch()
                if more:
                    time.sleep(0.05)
            else:
                wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
