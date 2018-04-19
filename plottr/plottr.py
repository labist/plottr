"""
plottr. A simple server application that can plot data streamed through
network sockets from other processes.

Author: Wolfgang Pfaff <wolfgangpfff@gmail.com>

TODO: (before releasing into the wild)
    * all constants should become configurable
    * launcher .bat or so.
    * examples
    * better checking if we can work with data that came in.
    * some tools for packaging the data correctly.
    * a qcodes subscriber.
    * docstrings everywhere public.
    * make some methods private.
"""

import sys
import time
from collections import OrderedDict

import numpy as np
import pandas as pd
import xarray as xr
import zmq
from matplotlib import rcParams
from matplotlib.backends.backend_qt5agg import (FigureCanvasQTAgg as FCanvas,
                                                NavigationToolbar2QT as NavBar, )
from matplotlib.figure import Figure
from PyQt5.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (QApplication, QComboBox, QDialog, QFormLayout,
                             QFrame, QGroupBox, QHBoxLayout, QLabel,
                             QMainWindow, QPlainTextEdit, QSizePolicy,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                             QWidget)

APPTITLE = "plottr"
AVGAXISNAMES = ['average', 'averages', 'repetition', 'repetitions']
PORT = 5557
TIMEFMT = "[%Y/%m/%d %H:%M:%S]"


def getTimestamp(timeTuple=None):
    if not timeTuple:
        timeTuple = time.localtime()
    return time.strftime(TIMEFMT, timeTuple)


def getAppTitle():
    return f"{APPTITLE}"


def setMplDefaults():
    rcParams['axes.grid'] = True
    rcParams['font.family'] = 'Arial'
    rcParams['font.size'] = 8
    rcParams['lines.markersize'] = 4
    rcParams['lines.linestyle'] = '-'
    rcParams['savefig.transparent'] = False


def dictToDataFrames(dataDict):
    dfs = []
    for n in dataDict:
        if 'axes' not in dataDict[n]:
            continue
        vals = dataDict[n]['values']
        coords = [ (a, dataDict[a]['values']) for a in dataDict[n]['axes']]

        mi = pd.MultiIndex.from_tuples(list(zip(*[v for n, v in coords])), names=dataDict[n]['axes'])
        df = pd.DataFrame(vals, mi)
        df.columns.name = n

        dfs.append(df)

    return dfs


def combineDataFrames(df1, df2, sortIndex=True):
    df = df1.append(df2)
    if sortIndex:
        df = df.sort_index()
    return df


def dataFrameToXArray(df):
    """
    Convert pandas DataFrame with MultiIndex to an xarray DataArray.
    """
    # conversion with MultiIndex leaves some residue; need to unstack the MI dimension.
    arr = xr.DataArray(df).unstack('dim_0').squeeze()

    # for tidiness, remove also any empty dimensions.
    for k, v in arr.coords.items():
        if not isinstance(v.values, np.ndarray):
            arr = arr.drop(k)
    return arr


class MPLPlot(FCanvas):

    def __init__(self, parent=None, width=4, height=3, dpi=150):
        self.fig = Figure(figsize=(width, height), dpi=dpi)

        # TODO: option for multiple subplots
        self.axes = self.fig.add_subplot(111)

        super().__init__(self.fig)

        self.setParent(parent)


class DataStructure(QTreeWidget):

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setColumnCount(2)
        self.setHeaderLabels(['Array', 'Properties'])
        self.setSelectionMode(QTreeWidget.SingleSelection)


class PlotChoice(QWidget):

    choiceUpdated = pyqtSignal()

    def __init__(self, parent=None):

        super().__init__(parent)

        self.avgSelection = QComboBox()
        self.xSelection = QComboBox()
        self.ySelection = QComboBox()

        axisChoiceBox = QGroupBox('Plot axes')
        axisChoiceLayout = QFormLayout()
        axisChoiceLayout.addRow(QLabel('Averaging axis'), self.avgSelection)
        axisChoiceLayout.addRow(QLabel('x axis'), self.xSelection)
        axisChoiceLayout.addRow(QLabel('y axis'), self.ySelection)
        axisChoiceBox.setLayout(axisChoiceLayout)

        mainLayout = QVBoxLayout(self)
        mainLayout.addWidget(axisChoiceBox)

        self.avgSelection.currentTextChanged.connect(self.avgSelected)
        self.xSelection.currentTextChanged.connect(self.xSelected)
        self.ySelection.currentTextChanged.connect(self.ySelected)

    @pyqtSlot(str)
    def avgSelected(self, val):
        self.updateOptions(self.avgSelection, val)

    @pyqtSlot(str)
    def xSelected(self, val):
        self.updateOptions(self.xSelection, val)

    @pyqtSlot(str)
    def ySelected(self, val):
        self.updateOptions(self.ySelection, val)

    def updateOptions(self, changedOption, newVal):
        """
        After changing the role of a data axis manually, we need to make
        sure this axis isn't used anywhere else.
        """
        for opt in self.avgSelection, self.xSelection, self.ySelection:
            if opt != changedOption and opt.currentText() == newVal:
                opt.setCurrentIndex(0)

        # TODO: axes > y still missing
        slices = [ slice(None, None, None) for n in self.axesNames[1:] ]
        self.choiceInfo = {
            'avgAxis' : self.avgSelection.currentIndex() - 1,
            'xAxis' : self.xSelection.currentIndex() - 1,
            'yAxis' : self.ySelection.currentIndex() - 1,
            'slices' : slices,
        }

        self.choiceUpdated.emit()

    def setOptions(self, dataStructure):
        """
        Populates the data choice widgets initially.
        """
        self.axesNames = [n for n, k in dataStructure['axes'].items() ]

        # Need an option that indicates that the choice is 'empty'
        self.noSelName = '<None>'
        while self.noSelName in self.axesNames:
            self.noSelName = '<' + self.noSelName + '>'
        self.axesNames.insert(0, self.noSelName)

        # check if some axis is obviously meant for averaging
        self.avgAxisName = None
        for n in self.axesNames:
            if n.lower() in AVGAXISNAMES:
                self.avgAxisName = n

        # add all options
        for opt in self.avgSelection, self.xSelection, self.ySelection:
            opt.clear()
            opt.addItems(self.axesNames)

        # select averaging axis automatically
        if self.avgAxisName:
            self.avgSelection.setCurrentText(self.avgAxisName)
        else:
            self.avgSelection.setCurrentIndex(0)

        # see which options remain for x and y, apply the first that work
        xopts = self.axesNames.copy()
        xopts.pop(0)
        if self.avgAxisName:
            xopts.pop(xopts.index(self.avgAxisName))

        if len(xopts) > 0:
            self.xSelection.setCurrentText(xopts[0])
        if len(xopts) > 1:
            self.ySelection.setCurrentText(xopts[1])

        self.choiceUpdated.emit()

class ProcessData(QObject):

    def __init__(self, parent=None):
        super().__init__(parent)

    def setDataFrame(self, df):
        self.df = df

    def process(self, info):
        print(info)
        # TODO: continue here.


class DataWindow(QMainWindow):

    def __init__(self, dataId=None, parent=None):
        super().__init__(parent)

        self.dataId = dataId
        self.setWindowTitle(getAppTitle() + f" ({dataId})")
        self.data = {}

        # TODO: somewhere here we should implement a choice of backend i feel.
        # plot settings
        setMplDefaults()

        # data chosing widgets
        self.structure = DataStructure()
        self.plotChoice = PlotChoice()
        chooserLayout = QVBoxLayout()
        chooserLayout.addWidget(self.structure)
        chooserLayout.addWidget(self.plotChoice)

        # plot control widgets
        self.plot = MPLPlot()
        plotLayout = QVBoxLayout()
        plotLayout.addWidget(self.plot)
        plotLayout.addWidget(NavBar(self.plot, self))

        # Main layout
        self.frame = QFrame()
        mainLayout = QHBoxLayout(self.frame)
        mainLayout.addLayout(chooserLayout)
        mainLayout.addLayout(plotLayout)

        # Data processing thread
        self.procData = ProcessData()
        self.dataThread = QThread()
        self.procData.moveToThread(self.dataThread)

        # signals/slots for data selection etc.
        self.structure.itemSelectionChanged.connect(self.dataSelected)
        self.plotChoice.choiceUpdated.connect(self.updatePlotData)

        self.dataThread.start()

        # activate window
        self.frame.setFocus()
        self.setCentralWidget(self.frame)
        self.activateWindow()

    @pyqtSlot()
    def dataSelected(self):
        sel = self.structure.selectedItems()
        if len(sel) == 1:
            self.activateData(sel[0].text(0))

        elif len(sel) == 0:
            self.plot.axes.clear()
            self.plot.draw()

    def activateData(self, name):
        self.plotChoice.setOptions(self.dataStructure[name])
        self.procData.setDataFrame(self.data[name])

        # mock plotting
        # TODO: replace
        # vals = xarr.values

        # axname = [ n for n, k in self.dataStructure[name]['axes'].items() ][0]
        # xvals = xarr.coords[axname].values

        # self.plot.axes.plot(xvals, vals, 'o')
        # self.plot.axes.set_xlabel(axname)
        # self.plot.axes.set_ylabel(name)
        # self.plot.draw()

    @pyqtSlot()
    def updatePlotData(self):
        self.procData.process(self.plotChoice.choiceInfo)


    def updateDataStructure(self, reset=True):
        curSelection = self.structure.selectedItems()
        if len(curSelection) > 0:
            selName = curSelection[0].text(0)
        else:
            selName = None

        if reset:
            self.structure.clear()
            for n, v in self.dataStructure.items():
                item = QTreeWidgetItem([n, '{} points'.format(v['nValues'])])
                for m, w in v['axes'].items():
                    childItem = QTreeWidgetItem([m, '{} points'.format(w['nValues'])])
                    childItem.setDisabled(True)
                    item.addChild(childItem)

                self.structure.addTopLevelItem(item)
                item.setExpanded(True)
                if selName and n == selName:
                    item.setSelected(True)

            if not selName:
                self.structure.topLevelItem(0).setSelected(True)

        else:
            raise NotImplementedError


    @pyqtSlot(dict)
    def addData(self, dataDict):
        doUpdate = dataDict.get('update', False)
        dataDict = dataDict.get('datasets', None)

        if dataDict:
            newDataFrames = dictToDataFrames(dataDict)
            if not doUpdate:
                self.dataStructure = {}
                for df in newDataFrames:
                    n = df.columns.name

                    self.dataStructure[n] = {}
                    self.dataStructure[n]['nValues'] = df.size
                    self.dataStructure[n]['axes'] = OrderedDict({})
                    for m, lvls in zip(df.index.names, df.index.levels):
                        self.dataStructure[n]['axes'][m] = {}
                        self.dataStructure[n]['axes'][m]['nValues'] = len(lvls)

                    self.data[n] = df
                    self.updateDataStructure(reset=True)
            else:
                raise NotImplementedError


class DataReceiver(QObject):

    sendInfo = pyqtSignal(str)
    sendData = pyqtSignal(dict)

    def __init__(self):
        super().__init__()

        context = zmq.Context()
        port = PORT
        self.socket = context.socket(zmq.PULL)
        self.socket.bind(f"tcp://127.0.0.1:{port}")
        self.running = True

    @pyqtSlot()
    def loop(self):
        self.sendInfo.emit("Listening...")

        while self.running:
            data = self.socket.recv_json()
            try:
                dataId = data['id']

            except KeyError:
                self.sendInfo.emit('Received invalid data (no ID)')
                continue

            # TODO: we probably should do some basic checking of the received data here.
            self.sendInfo.emit(f'Received data for dataset: {dataId}')
            self.sendData.emit(data)


class Logger(QPlainTextEdit):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)

    @pyqtSlot(str)
    def addMessage(self, msg):
        newMsg = "{} {}".format(getTimestamp(), msg)
        self.appendPlainText(newMsg)


class PlottrMain(QMainWindow):

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle(getAppTitle())
        self.setWindowIcon(QIcon('./plottr_icon.png'))
        self.activateWindow()

        # layout of basic widgets
        self.logger = Logger()
        self.frame = QFrame()
        layout = QVBoxLayout(self.frame)
        layout.addWidget(self.logger)

        # self.setLayout(layout)
        self.setCentralWidget(self.frame)
        self.frame.setFocus()

        # basic setup of the data handling
        self.dataHandlers = {}

        # setting up the Listening thread
        self.listeningThread = QThread()
        self.listener = DataReceiver()
        self.listener.moveToThread(self.listeningThread)

        # communication with the ZMQ thread
        self.listeningThread.started.connect(self.listener.loop)
        self.listener.sendInfo.connect(self.logger.addMessage)
        self.listener.sendData.connect(self.processData)

        # go!
        self.listeningThread.start()

    @pyqtSlot(dict)
    def processData(self, data):
        dataId = data['id']
        if dataId not in self.dataHandlers:
            self.dataHandlers[dataId] = DataWindow(dataId=dataId)
            self.dataHandlers[dataId].show()
            self.logger.addMessage(f'Started new data window for {dataId}')

        w = self.dataHandlers[dataId]
        w.addData(data)


    def closeEvent(self, event):
        self.listener.running = False
        self.listeningThread.quit()
        # self.listeningThread.wait()

        for d in self.dataHandlers:
            self.dataHandlers[d].close()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    main = PlottrMain()
    main.show()
    sys.exit(app.exec_())