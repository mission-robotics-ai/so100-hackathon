import tyro

from so100_hackathon.apis.calibrate import CalibrateConfig, main
from so100_hackathon.console import enable_pretty_tracebacks

if __name__ == "__main__":
    enable_pretty_tracebacks()
    main(tyro.cli(CalibrateConfig))
