from artiq.gui.scientific_spinbox import ScientificSpinBox
import logging
import re
from math import floor, log10

logger = logging.getLogger(__name__)

_exp_shorten = re.compile(r"e\+?0*")

def nDigits(value):
    if value == 0:
        return 1
    return floor(log10(abs(value))) + 1

class AdaptiveSpinBox(ScientificSpinBox):
    def setSigFigs(self, d=None):
        if d is None:
            d = self.decimals() + 3
        self._sig_figs = max(1, int(d))
        self._fmt = "{{:#.{}g}}".format(self._sig_figs)

    def textFromValue(self, v):
        text = super().textFromValue(v)
        return text.removesuffix('.')

    def stepBy(self, steps):
        cursorPosition = self.lineEdit().cursorPosition()
        nDigitsOutgoing = nDigits(self.value())

        if cursorPosition < len(self.prefix()) or cursorPosition > len(self.text()) - len(self.suffix()):
            return

        clean = self.text()
        if self.prefix():
            clean = clean.split(self.prefix(), 1)[-1]
        if self.suffix():
            clean = clean.rsplit(self.suffix(), 1)[0]

        cleanCursorPosition = max(cursorPosition - len(self.prefix()), 1)

        mantissa, exponent, *_ = (*clean.split('e', 1), 0)
        mantissa += '.' if '.' not in mantissa else ''

        sign = 1

        isExponentialForm = False

        if 'e' in clean:
            isExponentialForm = True

        if cleanCursorPosition <= clean.find('e') or clean.find('e') == -1:
            if cleanCursorPosition > mantissa.find('.') + 1:
                cleanCursorPosition -= 1
            if mantissa[0] == '-':
                mantissa = mantissa[1:]
                sign = -1
                cleanCursorPosition = max(1, cleanCursorPosition - 1)

            multiplier = 10**(len(mantissa) - mantissa.find('.') - 1)
            # logger.info(f"Multiplier: {multiplier}, Mantissa: {mantissa}, Cursor: {cleanCursorPosition}")
            mantissa = sign*int(mantissa.replace('.', '')) + 10**(len(mantissa) - cleanCursorPosition - 1) * steps
            # logger.info(f"Mantissa: {mantissa}")
            mantissa /= multiplier
        else:
            exponent = int(exponent) + steps

        self.setValue(float(f"{mantissa}e{exponent}"))

        nDigitsNew = nDigits(self.value())

        if cursorPosition < len(clean) + len(self.prefix()) and nDigitsNew <= 4:
            if cursorPosition - len(self.prefix()) == 0 and nDigitsNew > nDigitsOutgoing:
                self.lineEdit().setCursorPosition(cursorPosition + 1 + nDigitsNew - nDigitsOutgoing)
            else:
                self.lineEdit().setCursorPosition(cursorPosition + nDigitsNew - nDigitsOutgoing)
        
