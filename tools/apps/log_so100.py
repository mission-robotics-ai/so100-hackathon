import tyro

from so100_hackathon.apis.log_arms import LogArmsConfig, main
from so100_hackathon.console import enable_pretty_tracebacks

if __name__ == "__main__":
    enable_pretty_tracebacks()
    main(tyro.cli(LogArmsConfig))
