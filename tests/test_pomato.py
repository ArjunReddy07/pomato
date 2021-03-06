import logging
import random
import shutil
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from context import pomato, copytree	
           
# pylint: disable-msg=E1101
class TestPomatoMarketModel(unittest.TestCase):
    def setUp(self):
        self.wdir = Path.cwd().joinpath("examples")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(Path.cwd().joinpath("examples").joinpath("data_temp"), ignore_errors=True)
        shutil.rmtree(Path.cwd().joinpath("examples").joinpath("data_output"), ignore_errors=True)
        shutil.rmtree(Path.cwd().joinpath("examples").joinpath("logs"), ignore_errors=True)
        shutil.rmtree(Path.cwd().joinpath("examples").joinpath("domains"), ignore_errors=True)

    def test_run_ieee_init_invalid_option(self):
        mato = pomato.POMATO(wdir=self.wdir, options_file="INVALID_PATH",
                             logging_level=logging.ERROR)
        self.assertTrue(mato.options == pomato.tools.default_options())

    def test_run_ieee_init_no_option(self):
        mato = pomato.POMATO(wdir=self.wdir, logging_level=logging.ERROR)
        self.assertTrue(mato.options == pomato.tools.default_options())

    def test_run_ieee_init_invalid_data(self):
        mato = pomato.POMATO(wdir=self.wdir, logging_level=logging.ERROR)
        self.assertRaises(FileNotFoundError, mato.load_data, "INVALID_PATH")

    def test_init_ieee_mfile(self):
        mato = pomato.POMATO(wdir=self.wdir, logging_level=logging.ERROR)
        mato.load_data('data_input/pglib_opf_case118_ieee.m')

    def test_init_ieee_matfile(self):
        mato = pomato.POMATO(wdir=self.wdir, logging_level=logging.ERROR)
        mato.load_data('data_input/pglib_opf_case118_ieee.mat')

    def test_run_nrel(self):

        mato = pomato.POMATO(wdir=self.wdir, options_file="profiles/nrel118.json",
                             logging_level=logging.INFO)
        mato.load_data('data_input/nrel_118.zip')
        my_file = self.wdir.parent.joinpath('tests/test_data/cbco_nrel_118.csv')
        to_file = self.wdir.joinpath('data_temp/julia_files/cbco_data/cbco_nrel_118.csv')
        shutil.copyfile(str(my_file), str(to_file))

        R2_to_R3 = ["bus118", "bus076", "bus077", "bus078", "bus079", 
                    "bus080", "bus081", "bus097", "bus098", "bus099"]
        mato.data.nodes.loc[R2_to_R3, "zone"] = "R3"

        mato.options["optimization"]["timeseries"]["market_horizon"] = 10000
        mato.options["optimization"]["timeseries"]["redispatch_horizon"] = 24
        mato.options["optimization"]["constrain_nex"] = False
        mato.options["optimization"]["redispatch"]["include"] = True
        mato.options["optimization"]["redispatch"]["zones"] = list(mato.data.zones.index)
        mato.options["optimization"]["infeasibility"]["electricity"]["bound"] = 200
        mato.options["optimization"]["infeasibility"]["electricity"]["cost"] = 1000
        mato.options["optimization"]["redispatch"]["cost"] = 20
        mato.options["grid"]["capacity_multiplier"] = 1

        # %% NTC Model NEX = 0
        mato.data.results = {}
        mato.options["optimization"]["type"] = "ntc"
        mato.options["optimization"]["constrain_nex"] = True
        mato.data.set_default_net_position(0)
        mato.create_grid_representation()
        mato.grid_representation.ntc["ntc"] = \
            mato.grid_representation.ntc["ntc"]*0.01
        mato.update_market_model_data()
        mato.run_market_model()

        # NTC Model NTC = 100
        mato.data.results = {}
        mato.options["optimization"]["type"] = "ntc"
        mato.options["optimization"]["constrain_nex"] = False

        mato.create_grid_representation()
        mato.grid_representation.ntc["ntc"] = \
            mato.grid_representation.ntc["ntc"]*0.001
        mato.update_market_model_data()

        mato.run_market_model()

        # %% Zonal PTDF model
        mato.data.results = {}
        mato.options["optimization"]["type"] = "zonal"
        mato.options["grid"]["gsk"] = "gmax"

        mato.create_grid_representation()
        mato.update_market_model_data()
        mato.run_market_model()
        # %% Nodal PTDF model
        mato.data.results = {}
        mato.options["optimization"]["type"] = "nodal"

        mato.create_grid_representation()
        mato.update_market_model_data()
        mato.run_market_model()

        # %% FBMC basecase
        mato.data.results = {}
        mato.options["optimization"]["timeseries"]["market_horizon"] = 168
        mato.options["optimization"]["type"] = "cbco_nodal"
        mato.grid_model.options["grid"]["cbco_option"] = "clarkson_base"
        mato.options["optimization"]["redispatch"]["include"] = False
        mato.options["optimization"]["chance_constrained"]["include"] = False
        mato.options["grid"]["capacity_multiplier"] = 1
        mato.options["grid"]["sensitivity"] = 0.05

        mato.grid_model.options["grid"]["precalc_filename"] = "cbco_nrel_118"
        mato.create_grid_representation()
        mato.update_market_model_data()
        mato.run_market_model()
        result_name = next(r for r in list(mato.data.results))
        basecase = mato.data.results[result_name]
        mato.options["grid"]["minram"] = 0.1
        mato.options["grid"]["sensitivity"] = 0.05

        fbmc = pomato.fbmc.FBMCModule(mato.wdir, mato.grid, mato.data, mato.options)
        fbmc_gridrep = fbmc.create_flowbased_parameters(basecase, gsk_strategy="gmax", 
                                                        reduce=True)

        fbmc_domain = pomato.visualization.FBMCDomainPlots(mato.wdir, mato.grid, 
                                                           mato.data, mato.options, 
                                                           fbmc_gridrep)
        if not mato.wdir.joinpath("domains").is_dir():
            mato.wdir.joinpath("domains").mkdir()     
                          
        for t in basecase.INJ.t.unique():
            fbmc_domain.generate_flowbased_domain(["R1", "R2"], ["R1", "R3"], t, "nrel")
        fbmc_domain.save_all_domain_plots(mato.wdir.joinpath("domains"))

        # %% FBMC market clearing
        mato.data.results = {}
        mato.options["optimization"]["timeseries"]["market_horizon"] = 100
        mato.options["optimization"]["redispatch"]["include"] = True
        mato.options["optimization"]["redispatch"]["zones"] = list(mato.data.zones.index)
        mato.options["optimization"]["type"] = "nodal"
        mato.options["grid"]["capacity_multiplier"] = 1

        mato.create_grid_representation()
        mato.grid_representation.grid = fbmc_gridrep
        mato.options["optimization"]["type"] = "cbco_zonal"
        mato.options["optimization"]["constrain_nex"] = False
        mato.options["optimization"]["chance_constrained"]["include"] = False

        # mato.market_model.julia_model = None
        mato.update_market_model_data()
        mato.run_market_model()
        mato.create_geo_plot()

        # mato._join_julia_instance_market_model()
        mato._join_julia_instances()
