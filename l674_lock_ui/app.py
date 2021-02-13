from PyQt5 import QtWidgets, uic
import os
import sys


class UI(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        ui_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "main.ui")
        uic.loadUi(ui_path, self)


def main():
    app = QtWidgets.QApplication(sys.argv)
    ui = UI()
    ui.show()
    app.exec_()


if __name__ == "__main__":
    main()
