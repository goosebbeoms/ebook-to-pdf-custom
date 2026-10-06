import os
import sys
import time
import mss
import mss.tools
import tempfile
import threading

from pynput import mouse
from pynput.keyboard import Key, Controller
from PIL import Image

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QIntValidator, QPalette
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QLineEdit, QPushButton, QMainWindow, QVBoxLayout, \
    QHBoxLayout, QSlider, QFrame, QFileDialog, QMessageBox, QSystemTrayIcon, QStyle, QScrollArea

class CaptureCancelled(Exception):
    pass


class JobControl:
    """Cooperative cancellation and active-time waits, including PDF writes."""
    def __init__(self):
        self.condition = threading.Condition()
        self.paused = False
        self.cancelled = False

    def set_paused(self, paused):
        with self.condition:
            self.paused = paused
            self.condition.notify_all()

    def cancel(self):
        with self.condition:
            self.cancelled = True
            self.condition.notify_all()

    def wait(self, seconds, on_pause=None):
        remaining = seconds
        was_paused = False
        with self.condition:
            while True:
                if self.cancelled:
                    raise CaptureCancelled()
                if self.paused:
                    if not was_paused and on_pause:
                        on_pause()
                    was_paused = True
                    self.condition.wait()
                    continue
                if remaining <= 0:
                    return was_paused
                started = time.monotonic()
                self.condition.wait(min(remaining, 0.05))
                remaining -= time.monotonic() - started


class ControlledWriter:
    def __init__(self, stream, checkpoint):
        self.stream = stream
        self.checkpoint = checkpoint

    def write(self, data):
        self.checkpoint()
        return self.stream.write(data)

    def __getattr__(self, name):
        return getattr(self.stream, name)


class CaptureWorker(QThread):
    progress = Signal(str)
    outcome = Signal(str, str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.control = JobControl()
        self.capturing = True

    def wait_active(self, seconds=0):
        paused = self.control.wait(seconds, lambda: self.progress.emit('일시정지됨 · 재개 또는 중단을 선택하세요.'))
        if paused:
            if self.capturing:
                self.focus_viewer()
            else:
                self.progress.emit('PDF 변환 재개 중…')

    def focus_viewer(self):
        # The pause/resume button gives focus to this app. Allow the user to
        # uncover the viewer, then restore focus before sending any keys.
        while True:
            self.progress.emit('3초 후 캡처 준비 · 전자책 화면이 보이도록 전환해 주세요.')
            if not self.control.wait(3, lambda: self.progress.emit('일시정지됨')):
                break
        self.control.wait(0)
        pointer = mouse.Controller()
        previous = pointer.position
        try:
            region = self.settings['region']
            pointer.position = (region['left'], region['top'])
            pointer.click(mouse.Button.left)
        finally:
            pointer.position = previous
        self.wait_active(self.settings['speed'])

    def run(self):
        result = ('cancelled', '작업 중단 · 미완성 PDF와 임시 캡처를 정리했습니다.')
        try:
            settings = self.settings
            with tempfile.TemporaryDirectory(prefix='.ebook-capture-',
                                             dir=os.path.dirname(settings['pdf_path'])) as folder:
                self.focus_viewer()
                keyboard = Controller()
                images = []
                for page in range(1, settings['total'] + 1):
                    self.wait_active(settings['speed'])
                    self.progress.emit(f"캡처 중 · {page} / {settings['total']}페이지")
                    image_path = os.path.join(folder, f'{page:06d}.png')
                    with mss.mss() as screen:
                        shot = screen.grab(settings['region'])
                        mss.tools.to_png(shot.rgb, shot.size, output=image_path)
                    images.append(image_path)
                    self.wait_active()
                    if page < settings['total']:
                        if settings['refresh_enabled'] and page % settings['refresh_interval'] == 0:
                            keyboard.press(Key.f5)
                            keyboard.release(Key.f5)
                            delay = max(10, settings['refresh_wait'])
                            self.progress.emit(f'{page}페이지 캡처 완료 · 새로고침 후 {delay}초 대기 중')
                            self.wait_active(delay)
                        self.wait_active()
                        keyboard.press(Key.right)
                        keyboard.release(Key.right)

                self.capturing = False
                self.progress.emit('PDF 변환 중…')
                converted = []
                try:
                    for path in images:
                        self.wait_active()
                        with Image.open(path) as source:
                            converted.append(source.convert('RGB'))
                    staged_pdf = os.path.join(folder, 'result.pdf')
                    with open(staged_pdf, 'w+b') as stream:
                        converted[0].save(ControlledWriter(stream, self.wait_active), format='PDF',
                                          save_all=True, append_images=converted[1:], quality=100)
                    self.wait_active()
                    # Commit only a complete PDF. Cancellation preserves any existing file.
                    with self.control.condition:
                        self.control.wait(0)
                        os.replace(staged_pdf, settings['pdf_path'])
                finally:
                    for image in converted:
                        image.close()
            result = ('success', settings['pdf_path'])
        except CaptureCancelled:
            pass
        except Exception as error:
            result = ('error', f'작업 오류 · {error}')
        self.outcome.emit(*result)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        self.num = 1
        self.posX1 = 0
        self.posY1 = 0
        self.posX2 = 0
        self.posY2 = 0
        self.total_page = 1
        self.speed = 0.1
        self.region = {}
        self.file_list = []
        self.worker = None
        self.close_when_finished = False

        self.setWindowTitle("eBookToPdf · PDF Studio")
        self.resize(660, 850)
        self.setMinimumSize(560, 600)
        self.light_stylesheet = """
            QMainWindow, QWidget#canvas { background: #F3F5F4; }
            QWidget { color: #203B38; font-family: 'Malgun Gothic', 'Segoe UI'; font-size: 13px; }
            QLabel { background: transparent; }
            QLabel#title { font-size: 23px; font-weight: 700; color: #173D36; }
            QLabel#muted { color: #73827E; font-size: 12px; }
            QLabel#sectionTitle { font-size: 15px; font-weight: 700; }
            QLabel#step { background: #E7F3EE; color: #147666; border-radius: 10px; font-weight: 700; }
            QLabel#coordinate { color: #53716A; background: #F2F6F4; border-radius: 7px; padding: 6px 12px; }
            QLabel#speed { color: #147666; font-size: 16px; font-weight: 700; }
            QFrame#card { background: #FFFFFF; border: 1px solid #E0E8E3; border-radius: 16px; }
            QLineEdit { background: #F8FAF9; border: 1px solid #DCE5DF; border-radius: 9px; padding: 8px 13px; selection-background-color: #177F6E; selection-color: white; }
            QLineEdit:focus { border: 2px solid #268C78; padding: 7px 12px; background: #FFFFFF; }
            QPushButton { background: #FFFFFF; border: 1px solid #CCDCD3; border-radius: 9px; padding: 7px 16px; font-weight: 600; }
            QPushButton:hover { background: #EEF6F1; border-color: #7CAE98; }
            QPushButton:pressed { background: #DEECE3; }
            QPushButton:focus { border: 2px solid #268C78; }
            QPushButton#github { padding: 5px 8px; font-size: 12px; }
            QPushButton#refreshToggle { background: #E2EAE5; color: #53716A; border-radius: 12px; padding: 5px 14px; }
            QPushButton#refreshToggle:checked { background: #176B57; color: #FFFFFF; border-color: #176B57; }
            QPushButton#refreshToggle:hover { border-color: #268C78; }
            QPushButton#primary { background: #176B57; color: white; border: none; border-radius: 12px; font-size: 16px; padding: 12px; }
            QPushButton#primary:hover { background: #208369; }
            QPushButton#primary:pressed { background: #105440; }
            QPushButton#primary:focus { border: 2px solid #9BD0B9; }
            QPushButton#reset { background: transparent; border: none; color: #62796E; padding: 7px 10px; }
            QPushButton#reset:hover { background: #E3EBE5; }
            QPushButton#reset:focus { border: 1px solid #268C78; }
            QPushButton:disabled, QPushButton#primary:disabled { background: #E2EAE5; color: #8C9992; border: 1px solid #CCDCD3; }
            QSlider::groove:horizontal { background: #E2EAE5; height: 6px; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #21866E; border-radius: 3px; }
            QSlider::handle:horizontal { background: #FFFFFF; border: 3px solid #21866E; width: 14px; height: 14px; margin: -7px 0; border-radius: 10px; }
            QSlider::handle:horizontal:hover { border-color: #105440; }
            QSlider:focus { background: #EEF6F1; }
            QSlider::sub-page:horizontal:disabled { background: #CCD6D0; }
            QSlider::handle:horizontal:disabled { background: #F3F5F4; border-color: #ADBDB4; }
            QLabel:disabled, QLabel#speed:disabled { color: #8C9992; }
        """

        def label(text, name=None):
            widget = QLabel(text)
            if name:
                widget.setObjectName(name)
            return widget

        def card(number, title, description):
            frame = QFrame()
            frame.setObjectName("card")
            body = QVBoxLayout(frame)
            body.setContentsMargins(16, 12, 16, 12)
            body.setSpacing(8)
            heading = QHBoxLayout()
            heading.setSpacing(12)
            badge = label(number, "step")
            badge.setFixedSize(26, 26)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.addWidget(badge)
            heading.addWidget(label(title, "sectionTitle"))
            heading.addStretch()
            body.addLayout(heading)
            frame.setToolTip(description)
            return frame, body

        container = QWidget()
        container.setObjectName("canvas")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        self.title = label("E-Book PDF 생성기", "title")
        title_row = QHBoxLayout()
        title_row.addWidget(self.title, 1)
        self.theme_toggle = QPushButton("라이트 모드")
        self.theme_toggle.setCheckable(True)
        self.theme_toggle.setAccessibleName("다크 모드 활성화")
        self.theme_toggle.toggled.connect(self.apply_theme)
        title_row.addWidget(self.theme_toggle)
        self.github_button = QPushButton("goosebbeoms ↗")
        self.github_button.setObjectName("github")
        self.github_button.setToolTip("GitHub 프로필 열기 · https://github.com/goosebbeoms")
        self.github_button.setAccessibleName("개발자 goosebbeoms의 GitHub 프로필 열기")
        self.github_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/goosebbeoms")))
        title_row.addWidget(self.github_button)
        layout.addLayout(title_row)

        capture, capture_layout = card("01", "캡처 영역", "전자책 화면의 왼쪽 위와 오른쪽 아래 모서리를 차례로 지정하세요.")
        self.label1 = label("왼쪽 위")
        self.label2 = label("오른쪽 아래")
        self.label1_1 = label("(0, 0)", "coordinate")
        self.label2_1 = label("(0, 0)", "coordinate")
        self.button1 = QPushButton("위치 선택")
        self.button2 = QPushButton("위치 선택")
        for caption, value, button in ((self.label1, self.label1_1, self.button1), (self.label2, self.label2_1, self.button2)):
            row = QHBoxLayout()
            row.setSpacing(12)
            caption.setMinimumWidth(80)
            value.setMinimumWidth(115)
            row.addWidget(caption)
            row.addStretch()
            row.addWidget(value)
            row.addWidget(button)
            capture_layout.addLayout(row)
        self.button1.clicked.connect(self.좌측상단_좌표_클릭)
        self.button2.clicked.connect(self.우측하단_좌표_클릭)
        self.button1.setAccessibleName("캡처 영역 왼쪽 위 위치 선택")
        self.button2.setAccessibleName("캡처 영역 오른쪽 아래 위치 선택")
        layout.addWidget(capture)

        output, output_layout = card("02", "저장 설정", "페이지 수와 저장할 파일 이름을 입력하세요.")
        fields = QHBoxLayout()
        fields.setSpacing(16)
        self.label3 = label("총 페이지 수")
        self.label4 = label("PDF 파일 이름")
        self.input1 = QLineEdit()
        self.input1.setPlaceholderText("예: 120")
        self.input1.setValidator(QIntValidator(1, 999999, self))
        self.input1.setAccessibleName("총 페이지 수")
        self.input2 = QLineEdit()
        self.input2.setPlaceholderText("예: 나의 독서 기록")
        self.input2.setAccessibleName("PDF 파일 이름")
        self.label3.setBuddy(self.input1)
        self.label4.setBuddy(self.input2)
        for caption, field, stretch in ((self.label3, self.input1, 1), (self.label4, self.input2, 2)):
            column = QVBoxLayout()
            column.setSpacing(8)
            column.addWidget(caption)
            column.addWidget(field)
            fields.addLayout(column, stretch)
        output_layout.addLayout(fields)
        self.input2.setToolTip("선택한 폴더에 저장합니다. .pdf 확장자는 자동으로 붙습니다.")
        path_caption = label("저장 폴더")
        output_layout.addWidget(path_caption)
        path_row = QHBoxLayout()
        self.output_directory = QLineEdit(os.getcwd())
        self.output_directory.setReadOnly(True)
        self.output_directory.setToolTip(self.output_directory.text())
        self.output_directory.setCursorPosition(0)
        self.output_directory.setAccessibleName("PDF 저장 폴더")
        path_caption.setBuddy(self.output_directory)
        self.browse_button = QPushButton("폴더 선택")
        self.browse_button.clicked.connect(self.choose_output_directory)
        path_row.addWidget(self.output_directory, 1)
        path_row.addWidget(self.browse_button)
        output_layout.addLayout(path_row)
        layout.addWidget(output)

        timing, timing_layout = card("03", "캡처 간격", "페이지가 완전히 표시될 수 있도록 대기 시간을 조절하세요.")
        speed_row = QHBoxLayout()
        self.speed_label = label(f"{self.speed:.1f}초", "speed")
        self.speed_label.setMinimumWidth(52)
        self.speed_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(1, 50)
        self.speed_slider.setValue(1)
        self.speed_slider.setMinimumHeight(26)
        self.speed_slider.setAccessibleName("페이지당 캡처 대기 시간, 0.1초에서 5초")
        self.speed_slider.valueChanged.connect(self.속도_변경)
        self.speed_slider.setToolTip("페이지당 대기 시간: 0.1초(빠르게) ~ 5.0초(여유롭게)")
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_label)
        timing_layout.addLayout(speed_row)
        layout.addWidget(timing)

        refresh, refresh_layout = card("04", "자동 새로고침", "설정한 페이지 수만큼 캡처하면 F5로 브라우저를 새로고침합니다.")
        self.refresh_toggle = QPushButton("ON · 켜짐")
        self.refresh_toggle.setObjectName("refreshToggle")
        self.refresh_toggle.setCheckable(True)
        self.refresh_toggle.setChecked(True)
        self.refresh_toggle.setAccessibleName("자동 새로고침 활성화")
        self.refresh_toggle.setToolTip("끄면 새로고침과 새로고침 후 대기 없이 캡처합니다.")
        refresh_layout.itemAt(0).layout().addWidget(self.refresh_toggle)
        interval_row = QHBoxLayout()
        interval_caption = label("새로고침 주기 (5~100페이지)")
        refresh_layout.addWidget(interval_caption)
        self.refresh_interval = QSlider(Qt.Orientation.Horizontal)
        self.refresh_interval.setRange(5, 100)
        self.refresh_interval.setSingleStep(1)
        self.refresh_interval.setValue(35)
        self.refresh_interval.setMinimumHeight(26)
        self.refresh_interval.setAccessibleName("브라우저 새로고침 주기, 5페이지에서 100페이지")
        self.refresh_interval.setToolTip("기본 35페이지마다 새로고침합니다. 마지막 캡처 후에는 새로고침하지 않습니다.")
        interval_caption.setBuddy(self.refresh_interval)
        self.refresh_interval_label = label("35페이지", "speed")
        self.refresh_interval_label.setMinimumWidth(90)
        self.refresh_interval_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.refresh_interval.valueChanged.connect(
            lambda value: self.refresh_interval_label.setText(f"{value}페이지"))
        interval_row.addWidget(self.refresh_interval, 1)
        interval_row.addWidget(self.refresh_interval_label)
        refresh_layout.addLayout(interval_row)

        wait_caption = label("새로고침 후 대기 시간 (10~30초)")
        refresh_layout.addWidget(wait_caption)
        refresh_wait_row = QHBoxLayout()
        self.refresh_wait_slider = QSlider(Qt.Orientation.Horizontal)
        self.refresh_wait_slider.setRange(10, 30)
        self.refresh_wait_slider.setValue(10)
        self.refresh_wait_slider.setMinimumHeight(26)
        self.refresh_wait_slider.setAccessibleName("새로고침 후 대기 시간, 10초에서 30초")
        self.refresh_wait_label = label("10초", "speed")
        self.refresh_wait_label.setMinimumWidth(60)
        self.refresh_wait_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.refresh_wait_slider.valueChanged.connect(
            lambda value: self.refresh_wait_label.setText(f"{value}초"))
        refresh_wait_row.addWidget(self.refresh_wait_slider, 1)
        refresh_wait_row.addWidget(self.refresh_wait_label)
        refresh_layout.addLayout(refresh_wait_row)
        refresh_note = label("새로고침 후 현재 페이지가 유지되는 뷰어에서 사용하세요.", "muted")
        refresh_note.setWordWrap(True)
        refresh_layout.addWidget(refresh_note)
        self.refresh_controls = (
            interval_caption, self.refresh_interval, self.refresh_interval_label,
            wait_caption, self.refresh_wait_slider, self.refresh_wait_label, refresh_note)
        self.refresh_toggle.toggled.connect(self.set_refresh_enabled)
        layout.addWidget(refresh)

        footer = QHBoxLayout()
        self.stat = label("준비 완료 · 캡처 영역을 선택해 주세요.")
        self.stat.setWordWrap(True)
        footer.addWidget(self.stat, 1)
        self.button4 = QPushButton("초기화")
        self.button4.setObjectName("reset")
        self.button4.clicked.connect(self.초기화)
        footer.addWidget(self.button4)
        layout.addLayout(footer)
        self.button3 = QPushButton("PDF 만들기  →")
        self.button3.setObjectName("primary")
        self.button3.setToolTip("전자책 뷰어에서 오른쪽 방향키로 페이지가 넘어가는지 확인하세요.")
        self.button3.clicked.connect(self.btn_click)
        layout.addWidget(self.button3)
        task_controls = QHBoxLayout()
        self.pause_button = QPushButton('일시정지')
        self.pause_button.setToolTip('일시정지 중에는 전자책 페이지를 바꾸지 마세요. 재개 시 3초의 준비 시간 후 이어서 캡처합니다.')
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.stop_button = QPushButton('작업 중단')
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip('미완성 PDF와 이번 작업의 임시 캡처를 삭제합니다.')
        self.stop_button.clicked.connect(self.stop_capture)
        task_controls.addWidget(self.pause_button)
        task_controls.addWidget(self.stop_button)
        layout.addLayout(task_controls)
        layout.addStretch()
        for button in (self.button1, self.button2, self.button3, self.button4,
                       self.refresh_toggle, self.theme_toggle, self.browse_button,
                       self.pause_button, self.stop_button, self.github_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(container)
        # Keep task controls visible even when the settings need scrolling.
        layout.removeItem(footer)
        layout.removeWidget(self.button3)
        layout.removeItem(task_controls)
        canvas = QWidget()
        canvas.setObjectName('canvas')
        canvas_layout = QVBoxLayout(canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.addWidget(scroll, 1)
        controls_layout = QVBoxLayout()
        controls_layout.setContentsMargins(20, 0, 20, 18)
        controls_layout.addLayout(footer)
        controls_layout.addWidget(self.button3)
        controls_layout.addLayout(task_controls)
        canvas_layout.addLayout(controls_layout)
        self.setCentralWidget(canvas)
        self.setTabOrder(self.theme_toggle, self.github_button)
        self.setTabOrder(self.github_button, self.button1)
        self.setTabOrder(self.button1, self.button2)
        self.setTabOrder(self.button2, self.input1)
        self.setTabOrder(self.input1, self.input2)
        self.setTabOrder(self.input2, self.output_directory)
        self.setTabOrder(self.output_directory, self.browse_button)
        self.setTabOrder(self.browse_button, self.speed_slider)
        self.setTabOrder(self.speed_slider, self.refresh_toggle)
        self.setTabOrder(self.refresh_toggle, self.refresh_interval)
        self.setTabOrder(self.refresh_interval, self.refresh_wait_slider)
        self.setTabOrder(self.refresh_wait_slider, self.button3)
        self.setTabOrder(self.button3, self.pause_button)
        self.setTabOrder(self.pause_button, self.stop_button)
        self.setTabOrder(self.stop_button, self.button4)
        self.apply_theme(False)
        icon = QIcon(os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon.ico"))
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.setWindowIcon(icon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("E-Book PDF 생성기")

    def apply_theme(self, dark):
        colors = {
            "#F3F5F4": "#171E1C", "#203B38": "#DFEBE5", "#173D36": "#EDF8F2",
            "#73827E": "#A5B8AE", "#E7F3EE": "#283F35", "#147666": "#7CD8B8",
            "#53716A": "#B1C8BB", "#F2F6F4": "#293A32", "#FFFFFF": "#222D27",
            "#E0E8E3": "#3B4D42", "#F8FAF9": "#19251E", "#DCE5DF": "#4A6052",
            "#CCDCD3": "#496052", "#EEF6F1": "#30463A", "#7CAE98": "#79B699",
            "#DEECE3": "#3B5747", "#E2EAE5": "#394D41", "#62796E": "#B5CABC",
            "#E3EBE5": "#30463A", "#21866E": "#6BC4A3", "#CCD6D0": "#4B5B51",
            "#ADBDB4": "#65766B", "#8C9992": "#82958A",
        }
        stylesheet = self.light_stylesheet
        if dark:
            for light, night in colors.items():
                stylesheet = stylesheet.replace(light, night)
            # Filled controls and selections always use light foregrounds.
            stylesheet += "QPushButton#refreshToggle:checked { color: white; }"
        self.setStyleSheet(stylesheet)
        palette = QPalette()
        roles = {
            QPalette.ColorRole.Window: "#171E1C" if dark else "#F3F5F4",
            QPalette.ColorRole.Base: "#19251E" if dark else "#F8FAF9",
            QPalette.ColorRole.Button: "#222D27" if dark else "#FFFFFF",
            QPalette.ColorRole.Text: "#DFEBE5" if dark else "#203B38",
            QPalette.ColorRole.WindowText: "#DFEBE5" if dark else "#203B38",
            QPalette.ColorRole.ButtonText: "#DFEBE5" if dark else "#203B38",
            QPalette.ColorRole.Highlight: "#177F6E",
            QPalette.ColorRole.HighlightedText: "#FFFFFF",
            QPalette.ColorRole.PlaceholderText: "#A5B8AE" if dark else "#73827E",
        }
        for role, color in roles.items():
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.theme_toggle.setText("다크 모드" if dark else "라이트 모드")

    def choose_output_directory(self):
        folder = QFileDialog.getExistingDirectory(
            self, "PDF 저장 폴더 선택", self.output_directory.text())
        if folder:
            self.output_directory.setText(os.path.normpath(folder))
            self.output_directory.setToolTip(self.output_directory.text())
            self.output_directory.setCursorPosition(0)

    def get_output_path(self):
        name = self.input2.text().strip()
        if not name or any(char in '<>:"/\\|?*' or ord(char) < 32 for char in name):
            raise ValueError("PDF 파일 이름에 사용할 수 없는 문자가 있습니다.")
        if name.endswith((".", " ")):
            raise ValueError("PDF 파일 이름은 점이나 공백으로 끝날 수 없습니다.")
        if not name.lower().endswith('.pdf'):
            name += '.pdf'
        folder = self.output_directory.text()
        if not os.path.isdir(folder):
            raise ValueError("저장 폴더가 없습니다. 폴더를 다시 선택해 주세요.")
        return os.path.abspath(os.path.join(folder, name))

    def notify_pdf_complete(self, pdf_path):
        QApplication.alert(self)
        if QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages():
            self.tray_icon.show()
            self.tray_icon.showMessage("PDF 만들기 완료", f"저장 위치: {pdf_path}",
                                       QSystemTrayIcon.MessageIcon.Information, 10000)
        else:
            self.completion_message = QMessageBox(self)
            self.completion_message.setWindowTitle("PDF 만들기 완료")
            self.completion_message.setIcon(QMessageBox.Icon.Information)
            self.completion_message.setText(f"PDF 저장을 완료했습니다.\n{pdf_path}")
            self.completion_message.open()

    def 초기화(self):
        self.num = 1
        self.posX1 = 0
        self.posY1 = 0
        self.posX2 = 0
        self.posY2 = 0
        self.speed = 0.1
        self.total_page = 1
        self.region = {}
        self.label1_1.setText('(0, 0)')
        self.label2_1.setText('(0, 0)')
        self.input1.clear()
        self.input2.clear()
        self.stat.setText("준비 완료 · 캡처 영역을 선택해 주세요.")
        self.speed_slider.setValue(1)
        self.refresh_interval.setValue(35)
        self.refresh_wait_slider.setValue(10)
        self.refresh_toggle.setChecked(True)

    def set_refresh_enabled(self, enabled):
        self.refresh_toggle.setText("ON · 켜짐" if enabled else "OFF · 꺼짐")
        for control in self.refresh_controls:
            control.setEnabled(enabled and self.worker is None)

    def set_running(self, running):
        for control in (self.button1, self.button2, self.button3, self.button4,
                        self.input1, self.input2, self.output_directory, self.browse_button,
                        self.speed_slider, self.refresh_toggle):
            control.setEnabled(not running)
        for control in self.refresh_controls:
            control.setEnabled(not running and self.refresh_toggle.isChecked())
        self.pause_button.setEnabled(running)
        self.stop_button.setEnabled(running)
        self.pause_button.setText('일시정지')

    def toggle_pause(self):
        if self.worker is None or self.worker.control.cancelled:
            return
        paused = not self.worker.control.paused
        self.worker.control.set_paused(paused)
        self.pause_button.setText('재개' if paused else '일시정지')
        self.stat.setText('일시정지 요청 중…' if paused else '작업 재개 준비 중…')

    def stop_capture(self):
        if self.worker is None:
            return
        self.worker.control.cancel()
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.stat.setText('작업 중단 중 · 임시 파일을 정리하고 있습니다.')

    def update_progress(self, message):
        if self.worker is not None and not self.worker.control.cancelled:
            self.stat.setText(message)

    def capture_result(self, kind, message):
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        if kind == 'success':
            self.stat.setText(f'PDF 저장 완료 · {message}')
            if not self.close_when_finished:
                self.notify_pdf_complete(message)
        else:
            self.stat.setText(message)

    def capture_finished(self):
        worker = self.worker
        self.worker = None
        worker.deleteLater()
        self.set_running(False)
        if self.close_when_finished:
            self.close()

    def closeEvent(self, event):
        if self.worker is not None:
            self.close_when_finished = True
            self.stop_capture()
            event.ignore()
        else:
            event.accept()

    def 좌측상단_좌표_클릭(self):
        def on_click(x, y, button, pressed):
            self.posX1 = int(x)
            self.posY1 = int(y)
            self.label1_1.setText(str(f'({int(x)}, {int(y)})'))
            print('Button: %s, Position: (%s, %s), Pressed: %s ' % (button, x, y, pressed))
            if not pressed:
                return False

        with mouse.Listener(on_click=on_click) as listener:
            listener.join()

    def 우측하단_좌표_클릭(self):
        def on_click(x, y, button, pressed):
            self.posX2 = int(x)
            self.posY2 = int(y)
            self.label2_1.setText(str(f'({int(x)}, {int(y)})'))
            print('Button: %s, Position: (%s, %s), Pressed: %s ' % (button, x, y, pressed))
            if not pressed:
                return False

        with mouse.Listener(on_click=on_click) as listener:
            listener.join()
    def 속도_변경(self):
        self.speed = self.speed_slider.value() / 10.0
        self.speed_label.setText(f'{self.speed:.1f}초')

    def btn_click(self):
        if self.worker is not None:
            return

        if not self.input1.hasAcceptableInput():
            self.stat.setText('페이지 수를 입력하세요.')
            self.input1.setFocus()
            return

        if not self.input2.text().strip():
            self.stat.setText('PDF 제목을 입력하세요.')
            self.input2.setFocus()
            return

        if self.posX2 <= self.posX1 or self.posY2 <= self.posY1:
            self.stat.setText('캡처 영역의 왼쪽 위와 오른쪽 아래를 다시 선택해 주세요.')
            return

        try:
            pdf_path = self.get_output_path()
        except ValueError as error:
            self.stat.setText(str(error))
            return

        settings = {
            'pdf_path': pdf_path,
            'total': int(self.input1.text()),
            'speed': self.speed,
            'refresh_interval': self.refresh_interval.value(),
            'refresh_wait': self.refresh_wait_slider.value(),
            'refresh_enabled': self.refresh_toggle.isChecked(),
            'region': {'top': self.posY1, 'left': self.posX1,
                       'width': self.posX2 - self.posX1, 'height': self.posY2 - self.posY1},
        }
        self.worker = CaptureWorker(settings, self)
        self.worker.progress.connect(self.update_progress)
        self.worker.outcome.connect(self.capture_result)
        self.worker.finished.connect(self.capture_finished)
        self.set_running(True)
        self.stat.setText('캡처 준비 중…')
        self.worker.start()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
