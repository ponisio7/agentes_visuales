# ui/theme_manager.py - VERSIÓN COMPLETA
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPalette, QColor

class ThemeManager:
    """Gestor de temas claro/oscuro con aplicación real"""
    
    THEMES = {
        'dark': {
            'bg': '#1e1e1e',
            'fg': '#ffffff',
            'border': '#3d3d3d',
            'accent': '#0078d4',
            'input_bg': '#2d2d2d',
            'hover': '#2d2d2d',
            'shadow': '#000000'
        },
        'light': {
            'bg': '#ffffff',
            'fg': '#000000',
            'border': '#d1d5db',
            'accent': '#007bff',
            'input_bg': '#ffffff',
            'hover': '#e9ecef',
            'shadow': '#00000033'
        }
    }
    
    @staticmethod
    def aplicar_tema(theme: str):
        """Aplica un tema completo a la aplicación"""
        if theme not in ThemeManager.THEMES:
            return
        
        theme_data = ThemeManager.THEMES[theme]
        app = QApplication.instance()
        if not app:
            return
        
        # Paleta de colores
        palette = QPalette()
        
        # Colores base
        bg_color = QColor(theme_data['bg'])
        fg_color = QColor(theme_data['fg'])
        border_color = QColor(theme_data['border'])
        accent_color = QColor(theme_data['accent'])
        input_bg = QColor(theme_data['input_bg'])
        hover_color = QColor(theme_data['hover'])
        
        # Configurar paleta
        palette.setColor(QPalette.ColorRole.Window, bg_color)
        palette.setColor(QPalette.ColorRole.WindowText, fg_color)
        palette.setColor(QPalette.ColorRole.Base, input_bg)
        palette.setColor(QPalette.ColorRole.AlternateBase, bg_color)
        palette.setColor(QPalette.ColorRole.ToolTipBase, bg_color)
        palette.setColor(QPalette.ColorRole.ToolTipText, fg_color)
        palette.setColor(QPalette.ColorRole.Text, fg_color)
        palette.setColor(QPalette.ColorRole.Button, bg_color)
        palette.setColor(QPalette.ColorRole.ButtonText, fg_color)
        palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Highlight, accent_color)
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
        
        app.setPalette(palette)
        
        # Estilo CSS complementario
        if theme == 'dark':
            app.setStyleSheet("""
                QMainWindow, QWidget { background-color: #1e1e1e; color: #ffffff; }
                QGroupBox { 
                    border: 1px solid #3d3d3d; 
                    border-radius: 4px; 
                    margin-top: 10px;
                    padding-top: 10px;
                    color: #ffffff;
                }
                QGroupBox::title { color: #ffffff; }
                QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {
                    background-color: #2d2d2d;
                    color: #ffffff;
                    border: 1px solid #3d3d3d;
                    border-radius: 4px;
                    padding: 4px 8px;
                }
                QPushButton {
                    background-color: #2d2d2d;
                    color: #ffffff;
                    border: 1px solid #3d3d3d;
                    border-radius: 4px;
                    padding: 6px 12px;
                }
                QPushButton:hover { background-color: #3d3d3d; }
                QPushButton:disabled { 
                    background-color: #1a1a1a; 
                    color: #666666; 
                }
                QProgressBar {
                    border: 1px solid #3d3d3d;
                    border-radius: 3px;
                    text-align: center;
                    color: #ffffff;
                }
                QProgressBar::chunk {
                    background-color: #0078d4;
                    border-radius: 3px;
                }
                QTabWidget::pane {
                    border: 1px solid #3d3d3d;
                    border-radius: 4px;
                }
                QTabBar::tab {
                    padding: 6px 12px;
                    background-color: #2d2d2d;
                    border: 1px solid #3d3d3d;
                    border-bottom: none;
                    border-radius: 4px 4px 0 0;
                    color: #ffffff;
                }
                QTabBar::tab:selected {
                    background-color: #1e1e1e;
                    border-bottom: 1px solid #1e1e1e;
                }
                QScrollArea { 
                    background-color: #1e1e1e;
                    border: 1px solid #3d3d3d;
                    border-radius: 4px;
                }
                QLabel { color: #ffffff; }
                QTextEdit { background-color: #2d2d2d; color: #ffffff; }
                QMenuBar { background-color: #1e1e1e; color: #ffffff; }
                QMenuBar::item:selected { background-color: #2d2d2d; }
                QMenu { background-color: #2d2d2d; color: #ffffff; }
                QMenu::item:selected { background-color: #3d3d3d; }
            """)
        else:
            # Tema claro (el que ya tenías)
            app.setStyleSheet("""
                QMainWindow { background-color: #f0f2f5; }
                QGroupBox {
                    font-weight: bold;
                    border: 1px solid #d1d5db;
                    border-radius: 4px;
                    margin-top: 10px;
                    padding-top: 10px;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 5px 0 5px;
                }
                QPushButton {
                    border-radius: 4px;
                    padding: 6px 12px;
                }
                QPushButton:disabled {
                    background-color: #ccc !important;
                    color: #666 !important;
                }
                QLineEdit, QComboBox {
                    padding: 4px 8px;
                    border: 1px solid #d1d5db;
                    border-radius: 4px;
                    background-color: white;
                }
                QLineEdit:focus, QComboBox:focus {
                    border-color: #007bff;
                }
                QProgressBar {
                    border: 1px solid #d1d5db;
                    border-radius: 3px;
                    text-align: center;
                    height: 20px;
                }
                QProgressBar::chunk {
                    background-color: #007bff;
                    border-radius: 3px;
                }
                QScrollArea {
                    background-color: white;
                    border: 1px solid #d1d5db;
                    border-radius: 4px;
                }
                QTextEdit {
                    border: 1px solid #d1d5db;
                    border-radius: 4px;
                }
                QLabel { color: #333; }
                QTabWidget::pane {
                    border: 1px solid #d1d5db;
                    border-radius: 4px;
                }
                QTabBar::tab {
                    padding: 6px 12px;
                    background-color: #e9ecef;
                    border: 1px solid #d1d5db;
                    border-bottom: none;
                    border-radius: 4px 4px 0 0;
                }
                QTabBar::tab:selected {
                    background-color: white;
                }
            """)
    
    @staticmethod
    def toggle_theme():
        """Alterna entre tema claro y oscuro"""
        app = QApplication.instance()
        if not app:
            return
        
        current_palette = app.palette()
        # Detectar tema actual (heurística simple)
        bg = current_palette.color(QPalette.ColorRole.Window)
        if bg.lightness() > 128:
            ThemeManager.aplicar_tema('dark')
            return 'dark'
        else:
            ThemeManager.aplicar_tema('light')
            return 'light'