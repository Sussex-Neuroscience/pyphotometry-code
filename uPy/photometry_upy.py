# Code that runs on the pyboard which handles data acquisition and streaming.

import pyb
import gc
from array import array

class Photometry():

    def __init__(self, mode):
        assert mode in ['GCaMP/RFP', 'GCaMP/iso', 'GCaMP/RFP_dif'], 'Invalid mode.'
        self.mode = mode
        if mode == 'GCaMP/RFP': # 2 channel GFP/RFP acquisition mode.
            self.oversampling_rate = 3e5  # Hz.
        elif mode == 'GCaMP/iso': # GCaMP and isosbestic recorded on same channel using time division multiplexing.
            self.oversampling_rate = 128e3 # Hz.
        elif mode == 'GCaMP/RFP_dif': # GCaMP and RFP recorded using time division illumination and baseline subtraction..
            self.oversampling_rate = 256e3 # Hz.
        self.ADC1 = pyb.ADC('X11')
        self.ADC2 = pyb.ADC('X12')
        self.DI1 = pyb.Pin('X1', pyb.Pin.IN, pyb.Pin.PULL_DOWN)
        self.DI2 = pyb.Pin('X2', pyb.Pin.IN, pyb.Pin.PULL_DOWN)
        self.LED1 = pyb.DAC(1, bits=12)
        self.LED2 = pyb.DAC(2, bits=12)
        self.ovs_buffer = array('H',[0]*64) # Oversampling buffer
        self.ovs_timer = pyb.Timer(2)       # Oversampling timer.
        self.sampling_timer = pyb.Timer(3)
        self.usb_serial = pyb.USB_VCP()
        self.running = False
        self.set_LED_current(2,2)

    def set_LED_current(self, LED_1_current=None, LED_2_current=None):
        if LED_1_current: 
            self.LED_1_value = int(31*LED_1_current+46)
            if self.running and (self.mode == 'GCaMP/RFP'): 
                self.LED1.write(self.LED_1_value)
        if LED_2_current:
            self.LED_2_value = int(31*LED_2_current+46)
            if self.running and (self.mode == 'GCaMP/RFP'): 
                self.LED2.write(self.LED_2_value)

    def start(self, sampling_rate, buffer_size):
        # Start acquisition, stream data to computer, wait for ctrl+c over serial to stop. 
        # Setup sample buffers.
        self.buffer_size = buffer_size
        self.sample_buffers = (array('H',[0]*(buffer_size+2)), array('H',[0]*(buffer_size+2)))
        self.buffer_data_mv = (memoryview(self.sample_buffers[0])[:-2], 
                               memoryview(self.sample_buffers[1])[:-2])      
        self.sample = 0
        self.baseline = 0
        self.dig_sample = False
        self.write_buf = 0 # Buffer to write data to.
        self.send_buf  = 1 # Buffer to send data from.
        self.write_ind = 0 # Buffer index to write new data to. 
        self.buffer_ready = False # Set to True when full buffer is ready to send.
        self.running = True
        self.ovs_timer.init(freq=self.oversampling_rate)
        self.usb_serial.setinterrupt(-1) # Disable serial interrupt.
        gc.collect()
        gc.disable()
        if self.mode == 'GCaMP/RFP':
            self.sampling_timer.init(freq=sampling_rate)
            self.sampling_timer.callback(self.gcamp_rfp_ISR)
            self.LED1.write(self.LED_1_value)
            self.LED2.write(self.LED_2_value)
        elif self.mode == 'GCaMP/iso':
            self.sampling_timer.init(freq=sampling_rate*2)
            self.sampling_timer.callback(self.gcamp_iso_ISR)
        elif self.mode == 'GCaMP/RFP_dif':
            self.sampling_timer.init(freq=sampling_rate*2)
            self.sampling_timer.callback(self.gcamp_rfp_diff_ISR)
        while True:
            if self.buffer_ready:
                self._send_buffer()
            if self.usb_serial.any():
                self.recieved_byte = self.usb_serial.read(1)
                if self.recieved_byte == b'\xFF': # Stop signal.
                    break
                elif self.recieved_byte == b'\xFD': # Set LED 1 power.
                    self.set_LED_current(
                        LED_1_current=int.from_bytes(self.usb_serial.read(1), 'little'))
                elif self.recieved_byte == b'\xFE': # Set LED 2 power.
                    self.set_LED_current(
                        LED_2_current=int.from_bytes(self.usb_serial.read(1), 'little'))      
        self.stop()

    def stop(self):
        # Stop aquisition
        self.sampling_timer.deinit()
        self.ovs_timer.deinit()
        self.LED1.write(0)
        self.LED2.write(0)
        self.running = False
        self.usb_serial.setinterrupt(3) # Enable serial interrupt.
        gc.enable()

    @micropython.native
    def gcamp_rfp_ISR(self, t):
        # Interrupt service routine for GCamp/RFP acquisition mode, reads a sample from ADCs 
        # 1 and 2 sequentially, along with the two digital inputs. Analog signals are stored
        # in the 15 most significant bits of the sample buffer, digital signal in least
        # significant bit.
        self.ADC1.read_timed(self.ovs_buffer, self.ovs_timer)
        self.sample = sum(self.ovs_buffer) >> 3
        self.sample_buffers[self.write_buf][self.write_ind] = (self.sample << 1) | self.DI1.value()
        self.write_ind += 1
        self.ADC2.read_timed(self.ovs_buffer, self.ovs_timer)
        self.sample = sum(self.ovs_buffer) >> 3
        self.sample_buffers[self.write_buf][self.write_ind] = (self.sample << 1) | self.DI2.value()
        # Update write index and switch buffers if full.
        self.write_ind = (self.write_ind + 1) % self.buffer_size
        if self.write_ind == 0: # Buffer full, switch buffers.
            self.write_buf = 1 - self.write_buf
            self.send_buf  = 1 - self.send_buf
            self.buffer_ready = True

    @micropython.native
    def gcamp_iso_ISR(self, t):
        # Interrupt service routine for 2 channel GCamp / isosbestic acquisition mode. 
        if self.write_ind % 2:   # Odd samples are isosbestic illumination.
            self.LED2.write(self.LED_2_value) # Turn on 405nm illumination.
        else:                    # Even samples are blue illumination.
            self.LED1.write(self.LED_1_value) # Turn on 470nm illumination.
        pyb.udelay(350)          # Wait before reading ADC (us).
        # Acquire sample and store in buffer.
        self.ADC1.read_timed(self.ovs_buffer, self.ovs_timer)
        self.sample = sum(self.ovs_buffer) >> 3
        if self.write_ind % 2:
            self.LED2.write(0) # Turn off 405nm illumination.
            self.sample_buffers[self.write_buf][self.write_ind] = (self.sample << 1) | self.DI2.value()
        else:
            self.LED1.write(0) # Turn on 470nm illumination.
            self.sample_buffers[self.write_buf][self.write_ind] = (self.sample << 1) | self.DI1.value()
        # Update write index and switch buffers if full.
        self.write_ind = (self.write_ind + 1) % self.buffer_size
        if self.write_ind == 0: # Buffer full, switch buffers.
            self.write_buf = 1 - self.write_buf
            self.send_buf  = 1 - self.send_buf
            self.buffer_ready = True

    @micropython.native
    def gcamp_rfp_diff_ISR(self, t):
        # Interrupt service routine for 2 channel GCamp / RFP with baseline subtraction acquisition mode.
        if self.write_ind % 2:   # Odd samples are RFP illumination.
            self.ADC2.read_timed(self.ovs_buffer, self.ovs_timer)
            self.LED2.write(self.LED_2_value) # Turn on 560nm illumination.
        else:                    # Even samples are blue illumination.
            self.ADC1.read_timed(self.ovs_buffer, self.ovs_timer)
            self.LED1.write(self.LED_1_value) # Turn on 470nm illumination.
        self.baseline = sum(self.ovs_buffer) >> 3            
        pyb.udelay(300) # Wait before reading ADC (us).
        # Acquire sample, subtract baseline, store in buffer. 
        if self.write_ind % 2:
            self.ADC2.read_timed(self.ovs_buffer, self.ovs_timer)
            self.LED2.write(0) # Turn off 405nm illumination.
            self.dig_sample = self.DI2.value()
        else:
            self.ADC1.read_timed(self.ovs_buffer, self.ovs_timer)
            self.LED1.write(0) # Turn on 470nm illumination.
            self.dig_sample =self.DI1.value()
        self.sample = sum(self.ovs_buffer) >> 3
        self.sample = max(self.sample - self.baseline, 0)
        self.sample_buffers[self.write_buf][self.write_ind] = (self.sample << 1) | self.dig_sample
        # Update write index and switch buffers if full.
        self.write_ind = (self.write_ind + 1) % self.buffer_size
        if self.write_ind == 0: # Buffer full, switch buffers.
            self.write_buf = 1 - self.write_buf
            self.send_buf  = 1 - self.send_buf
            self.buffer_ready = True

    @micropython.native
    def _send_buffer(self):
        # Send full buffer to host computer. Format of the serial chunks sent to the computer: 
        # buffer[:-2] = data, buffer[-2] = checksum, buffer[-1] = 0.
        if self.usb_serial.any() and self.usb_serial.read(1) == b'\xFF':
                self.stop()
        self.sample_buffers[self.send_buf][-2] = sum(self.buffer_data_mv[self.send_buf]) # Checksum
        self.usb_serial.send(self.sample_buffers[self.send_buf])
        self.buffer_ready = False